# netbox/scripts/commission_device.py
#
# NetBox 3.7.6 Custom Script:
# - Create device
# - Allocate next available IPs from site/tag-matched prefixes based on Interface.label
# - Set mgmt_subnet IP as primary IPv4
# - Optionally create cables based on a user-provided patch plan mapping:
#     eth0=switch-1:Ethernet1/1
#     eth1=switch-2:Ethernet1/10
#
# Notes:
# - Prefix selection uses tags on Prefix matching interface labels.
# - Sorting prefixes by prefixlen is done in Python (no prefix_length DB field in 3.7.x).

from extras.scripts import Script, StringVar, ObjectVar, ChoiceVar, BooleanVar, TextVar
from utilities.exceptions import AbortScript

from dcim.models import Device, DeviceRole, DeviceType, Platform, Site, Interface, Cable
from ipam.models import IPAddress, Prefix
from tenancy.models import Tenant


class CommissionDevice(Script):
    class Meta:
        name = "Commission New Device (IP Allocation + Patch Plan Cabling)"
        description = (
            "Creates a device, allocates IPs from site/tag-matched prefixes based on interface labels, "
            "sets mgmt as primary IPv4, and optionally cables interfaces using a patch plan mapping."
        )
        field_order = (
            "hostname",
            "site",
            "platform",
            "device_type",
            "role",
            "tenant",
            "status",
            "allocate_all_labeled_interfaces",
            "allow_global_prefix_fallback",
            "create_cables_from_patch_plan",
            "enforce_patch_plan_site_match",
            "patch_plan",
        )

    # ----------------------------
    # Form fields
    # ----------------------------

    hostname = StringVar(
        label="Device hostname",
        required=True,
    )

    site = ObjectVar(
        model=Site,
        label="Site",
        required=True,
    )

    platform = ObjectVar(
        model=Platform,
        label="Platform",
        required=True,
    )

    device_type = ObjectVar(
        model=DeviceType,
        label="Device Type",
        required=True,
    )

    role = ObjectVar(
        model=DeviceRole,
        label="Device Role (optional)",
        required=False,
    )

    tenant = ObjectVar(
        model=Tenant,
        label="Tenant (optional)",
        required=False,
    )

    status = ChoiceVar(
        label="Device Status",
        choices=(
            ("active", "Active"),
            ("staged", "Staged"),
            ("planned", "Planned"),
        ),
        default="staged",
        required=True,
    )

    allocate_all_labeled_interfaces = BooleanVar(
        label="Allocate IPs for all labeled interfaces",
        default=True,
        required=True,
    )

    allow_global_prefix_fallback = BooleanVar(
        label="Allow global prefix fallback (site is empty)",
        description=(
            "If no prefix is found for the selected site + tag, allow using a global prefix (Prefix.site is empty) "
            "with the same tag."
        ),
        default=False,
        required=True,
    )

    create_cables_from_patch_plan = BooleanVar(
        label="Create cables from patch plan",
        description="If enabled, creates cables based on the patch_plan mappings.",
        default=False,
        required=True,
    )

    enforce_patch_plan_site_match = BooleanVar(
        label="Enforce B-side devices are in selected site",
        description="If enabled, abort if a referenced B-side device is not in the same site as the new device.",
        default=True,
        required=True,
    )

    patch_plan = TextVar(
        label="Patch plan mappings (optional)",
        required=False,
        description=(
            "One mapping per line. Format:\n"
            "  <A_INTERFACE>=<B_DEVICE>:<B_INTERFACE>\n\n"
            "Examples:\n"
            "  eth0=switch-1:Ethernet1/1\n"
            "  eth1=switch-2:Ethernet1/10\n\n"
            "Interface names must match EXACT NetBox interface names."
        ),
    )

    # ----------------------------
    # Allocation configuration
    # ----------------------------

    SUBNET_LABELS = (
        "mgmt_subnet",
        "dmz_a_subnet",
        "dmz_b_subnet",
        "rtp_subnet",
    )
    MGMT_LABEL = "mgmt_subnet"

    # ----------------------------
    # Helpers
    # ----------------------------

    def _normalize(self, s: str) -> str:
        return (s or "").strip()

    def _find_prefix(self, site: Site, tag_name: str, allow_global_fallback: bool) -> Prefix:
        qs_site = Prefix.objects.filter(site=site, tags__name=tag_name)

        if qs_site.exists():
            candidates = list(qs_site)
            scope = f"site={site}"
        elif allow_global_fallback:
            qs_global = Prefix.objects.filter(site__isnull=True, tags__name=tag_name)
            if qs_global.exists():
                candidates = list(qs_global)
                scope = "site=NULL (global)"
                self.log_warning(
                    f"No site-scoped prefix found for site='{site}' tag='{tag_name}'. Falling back to global pool."
                )
            else:
                candidates = []
                scope = None
        else:
            candidates = []
            scope = None

        if not candidates:
            site_prefix_count = Prefix.objects.filter(site=site).count()
            tag_prefix_count = Prefix.objects.filter(tags__name=tag_name).count()
            raise AbortScript(
                f"No prefix found for site='{site}' with tag='{tag_name}'. "
                f"Debug: prefixes at site='{site}': {site_prefix_count}, "
                f"prefixes with tag='{tag_name}': {tag_prefix_count}. "
                "Fix Prefix.site and Prefix.tags (or enable global fallback)."
            )

        candidates.sort(key=lambda p: (p.prefix.prefixlen, str(p.prefix)), reverse=True)
        chosen = candidates[0]
        self.log_info(f"Using prefix {chosen.prefix} ({scope}, tag={tag_name})")
        return chosen

    def _allocate_next_ip_str(self, prefix: Prefix) -> str:
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        return str(ip)

    def _assign_ip_to_interface(self, iface: Interface, prefix: Prefix, tenant: Tenant = None) -> IPAddress:
        addr = self._allocate_next_ip_str(prefix)
        ip_obj = IPAddress(
            address=addr,
            vrf=prefix.vrf,
            tenant=tenant or prefix.tenant,
            status="active",
            assigned_object=iface,
        )
        ip_obj.full_clean()
        ip_obj.save()
        return ip_obj

    def _parse_patch_plan(self, text: str):
        """
        Parse lines like:
          eth0=switch-1:Ethernet1/1
        Returns list of tuples: (a_iface_name, b_device_name, b_iface_name)
        """
        mappings = []
        if not text:
            return mappings

        for idx, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if "=" not in line or ":" not in line:
                raise AbortScript(
                    f"Patch plan line {idx} is invalid: '{raw}'. "
                    "Expected format: <A_INTERFACE>=<B_DEVICE>:<B_INTERFACE>"
                )

            left, right = line.split("=", 1)
            b_dev, b_if = right.split(":", 1)

            a_iface = self._normalize(left)
            b_device = self._normalize(b_dev)
            b_iface = self._normalize(b_if)

            if not a_iface or not b_device or not b_iface:
                raise AbortScript(f"Patch plan line {idx} has empty values: '{raw}'")

            mappings.append((a_iface, b_device, b_iface))

        return mappings

    def _create_cable(self, a_iface: Interface, b_iface: Interface):
        # Skip if already cabled
        if getattr(a_iface, "cable", None) or getattr(b_iface, "cable", None):
            self.log_info(
                f"Skipping (already cabled): {a_iface.device.name}:{a_iface.name} <-> {b_iface.device.name}:{b_iface.name}"
            )
            return False

        Cable.objects.create(
            a_terminations=[a_iface],
            b_terminations=[b_iface],
            status="connected",
        )
        self.log_success(
            f"Cabled: {a_iface.device.name}:{a_iface.name} <-> {b_iface.device.name}:{b_iface.name}"
        )
        return True

    # ----------------------------
    # Main
    # ----------------------------

    def run(self, data, commit):
        hostname = self._normalize(data["hostname"])
        site = data["site"]
        platform = data["platform"]
        device_type = data["device_type"]
        role = data.get("role")
        tenant = data.get("tenant")
        status = data["status"]

        allocate_all = data["allocate_all_labeled_interfaces"]
        allow_global_fallback = data["allow_global_prefix_fallback"]

        do_cabling = data["create_cables_from_patch_plan"]
        enforce_site_match = data["enforce_patch_plan_site_match"]
        patch_plan_text = data.get("patch_plan")

        if Device.objects.filter(name=hostname).exists():
            raise AbortScript(f"Device name '{hostname}' already exists in NetBox.")

        if not commit:
            self.log_info("[DRY-RUN] Would create device, allocate IPs, and optionally create cables.")
            return "Dry-run complete."

        # ---- create device ----
        device = Device(
            name=hostname,
            site=site,
            platform=platform,
            device_type=device_type,
            role=role,
            tenant=tenant,
            status=status,
        )
        device.full_clean()
        device.save()
        self.log_success(f"Created device: {device.name} (site={site}, type={device_type}, platform={platform})")

        # ---- ensure interfaces exist ----
        interfaces = list(device.interfaces.all())
        if not interfaces:
            raise AbortScript(
                "No interfaces found on device after creation. "
                "Check that the Device Type has interface templates defined."
            )

        # ---- IP allocation ----
        target_labels = set(self.SUBNET_LABELS) if allocate_all else {self.MGMT_LABEL}

        target_ifaces = []
        for iface in interfaces:
            label = self._normalize(getattr(iface, "label", None))
            if label in target_labels:
                target_ifaces.append(iface)

        if not target_ifaces:
            raise AbortScript(
                "No interfaces matched the required labels. "
                "Ensure Interface.label matches one of: "
                f"{', '.join(sorted(target_labels))}"
            )

        mgmt_ip_obj = None
        for iface in target_ifaces:
            label = self._normalize(iface.label)
            prefix = self._find_prefix(site, label, allow_global_fallback)
            ip_obj = self._assign_ip_to_interface(iface, prefix, tenant=tenant)
            self.log_success(
                f"Assigned {ip_obj.address} to {device.name}/{iface.name} (label={label}, prefix={prefix.prefix})"
            )
            if label == self.MGMT_LABEL:
                mgmt_ip_obj = ip_obj

        if mgmt_ip_obj:
            device.primary_ip4 = mgmt_ip_obj
            device.save()
            self.log_success(f"Set primary IPv4 for {device.name} to {mgmt_ip_obj.address}")
        else:
            self.log_warning(f"No '{self.MGMT_LABEL}' interface allocated; primary IPv4 not set.")

        # ---- Cabling from patch plan ----
        if do_cabling:
            mappings = self._parse_patch_plan(patch_plan_text)
            if not mappings:
                raise AbortScript("Cabling enabled but patch plan is empty. Add at least one mapping line.")

            # Build lookup for A-side interfaces on the newly created device
            a_if_by_name = {i.name: i for i in device.interfaces.all()}

            created = 0
            skipped = 0

            for a_name, b_dev_name, b_if_name in mappings:
                a_iface = a_if_by_name.get(a_name)
                if not a_iface:
                    raise AbortScript(
                        f"A-side interface '{a_name}' not found on new device '{device.name}'. "
                        "Check the interface name matches exactly in NetBox."
                    )

                # Find B-side device by name
                b_dev_qs = Device.objects.filter(name=b_dev_name)
                if not b_dev_qs.exists():
                    raise AbortScript(f"B-side device '{b_dev_name}' not found in NetBox.")
                b_dev = b_dev_qs.first()

                if enforce_site_match and b_dev.site_id != site.id:
                    raise AbortScript(
                        f"B-side device '{b_dev.name}' is in site '{b_dev.site}', not '{site}'."
                    )

                # Find B-side interface
                b_iface_qs = Interface.objects.filter(device=b_dev, name=b_if_name)
                if not b_iface_qs.exists():
                    raise AbortScript(
                        f"B-side interface '{b_if_name}' not found on device '{b_dev.name}'. "
                        "Check interface name matches exactly in NetBox."
                    )
                b_iface = b_iface_qs.first()

                # Create cable
                ok = self._create_cable(a_iface, b_iface)
                if ok:
                    created += 1
                else:
                    skipped += 1

            self.log_info(f"Patch plan cabling summary: created={created}, skipped={skipped}")

        return f"Commissioning complete for {device.name}."
