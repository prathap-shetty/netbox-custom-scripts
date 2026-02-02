# netbox/scripts/commission_device.py
#
# NetBox 3.7.6 Custom Script: Commission a new device + allocate IPs from site/tag-matched prefixes
# + optional auto-cabling to a B-side device (same site) by matching interface names.
#
# Requirements / conventions:
# - Prefixes are tagged with: mgmt_subnet, dmz_a_subnet, dmz_b_subnet, rtp_subnet
# - Prefix.site is set (preferred). If not set, script can optionally fall back to global (site NULL) pools.
# - Interfaces on the device have Interface.label set to one of the subnet labels above
#   (best done via DeviceType interface templates).
# - Auto-cabling: interface names must match exactly between A and B devices.

from extras.scripts import Script, StringVar, ObjectVar, ChoiceVar, BooleanVar
from utilities.exceptions import AbortScript

from dcim.models import Device, DeviceRole, DeviceType, Platform, Site, Interface, Cable
from ipam.models import IPAddress, Prefix
from tenancy.models import Tenant


class CommissionDevice(Script):
    class Meta:
        name = "Commission New Device (IP Allocation + Optional Cabling)"
        description = (
            "Creates a device, allocates next available IPs from site/tag-matched prefixes based on interface labels, "
            "sets mgmt IP as primary IPv4, and optionally cables all ports to a B-side device in the same site."
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
            "make_peer_connections",
            "b_device",
        )

    # ----------------------------
    # Form fields
    # ----------------------------

    hostname = StringVar(
        label="Device hostname",
        description="Name to assign to the device (must be unique in NetBox)",
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
        description=(
            "If enabled, any interface whose label matches one of the known subnet tags will get an IP. "
            "If disabled, only mgmt_subnet will be allocated."
        ),
        default=True,
        required=True,
    )

    allow_global_prefix_fallback = BooleanVar(
        label="Allow global prefix fallback (site is empty)",
        description=(
            "If no prefix is found for the selected site + tag, allow using a global prefix (Prefix.site is empty) "
            "with the same tag. Useful if your environment doesn't populate Prefix.site."
        ),
        default=False,
        required=True,
    )

    make_peer_connections = BooleanVar(
        label="Connect interfaces to B-side device",
        description="If enabled, cables all interfaces to the selected B-side device by matching interface names.",
        default=False,
        required=True,
    )

    b_device = ObjectVar(
        label="B-side device (same site)",
        model=Device,
        required=False,
        query_params={
            "site_id": "$site",
        },
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

    def _normalize_label(self, s: str) -> str:
        # Keep it simple; adjust if you want '-' and '_' normalization, etc.
        return (s or "").strip()

    def _find_prefix(self, site: Site, tag_name: str, allow_global_fallback: bool) -> Prefix:
        """
        Find a prefix for (site, tag). If multiple exist, pick most specific (largest prefixlen).
        Optionally fall back to global (site NULL) prefix with the same tag.
        NetBox 3.7.x doesn't have prefix_length field, so sort in Python.
        """
        qs_site = Prefix.objects.filter(site=site, tags__name=tag_name)

        if qs_site.exists():
            candidates = list(qs_site)
            scope = f"site={site.slug if hasattr(site,'slug') else site}"
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
                "Fix by tagging the correct Prefix and/or setting Prefix.site (or enable global fallback)."
            )

        # Most specific first
        candidates.sort(key=lambda p: (p.prefix.prefixlen, str(p.prefix)), reverse=True)
        chosen = candidates[0]

        self.log_info(f"Using prefix {chosen.prefix} ({scope}, tag={tag_name})")
        return chosen

    def _allocate_next_ip_str(self, prefix: Prefix) -> str:
        """
        Get the next available IP from a prefix as a string with mask.
        """
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        return str(ip)

    def _assign_ip_to_interface(self, iface: Interface, prefix: Prefix, tenant: Tenant = None) -> IPAddress:
        """
        Create and assign a new IPAddress to an interface using the next available IP from the prefix.
        """
        addr = self._allocate_next_ip_str(prefix)

        ip_obj = IPAddress(
            address=addr,
            vrf=prefix.vrf,
            tenant=tenant or prefix.tenant,
            status="active",
            assigned_object=iface,
        )
        # Validate before save
        ip_obj.full_clean()
        ip_obj.save()
        return ip_obj

    def _connect_device_to_peer_by_name(self, device_a: Device, device_b: Device):
        """
        Create cables between device_a and device_b by matching interface names (A.name == B.name).
        Skips interfaces that don't exist on B or are already cabled.
        """
        a_ifaces = list(device_a.interfaces.all())
        b_by_name = {i.name: i for i in device_b.interfaces.all()}

        created = 0
        skipped = 0

        for a in a_ifaces:
            b = b_by_name.get(a.name)
            if not b:
                self.log_warning(f"No matching B-side interface for {device_a.name}:{a.name}")
                skipped += 1
                continue

            # Skip if either side already has a cable
            if getattr(a, "cable", None) or getattr(b, "cable", None):
                self.log_info(f"Skipping (already cabled): {device_a.name}:{a.name} <-> {device_b.name}:{b.name}")
                skipped += 1
                continue

            Cable.objects.create(
                a_terminations=[a],
                b_terminations=[b],
                status="connected",
            )
            self.log_success(f"Cabled: {device_a.name}:{a.name} <-> {device_b.name}:{b.name}")
            created += 1

        self.log_info(f"Cabling summary: created={created}, skipped={skipped}")

    # ----------------------------
    # Main
    # ----------------------------

    def run(self, data, commit):
        hostname = data["hostname"].strip()
        site = data["site"]
        platform = data["platform"]
        device_type = data["device_type"]
        role = data.get("role")
        tenant = data.get("tenant")
        status = data["status"]

        allocate_all = data["allocate_all_labeled_interfaces"]
        allow_global_fallback = data["allow_global_prefix_fallback"]

        make_peer = data["make_peer_connections"]
        b_device = data.get("b_device")

        # ---- basic validations ----
        if Device.objects.filter(name=hostname).exists():
            raise AbortScript(f"Device name '{hostname}' already exists in NetBox.")

        if make_peer and not b_device:
            raise AbortScript("B-side device is required when 'Connect interfaces to B-side device' is enabled.")

        if make_peer and b_device.site_id != site.id:
            raise AbortScript("B-side device must be in the same site as the new device.")

        if not commit:
            self.log_info("[DRY-RUN] Would create device, allocate IPs, and optionally cable ports.")
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

        # Interfaces should now exist (instantiated from DeviceType templates)
        interfaces = list(device.interfaces.all())
        if not interfaces:
            raise AbortScript(
                "No interfaces found on device after creation. "
                "Check that the Device Type has interface templates defined."
            )

        # ---- IP allocation ----
        target_labels = set(self.SUBNET_LABELS) if allocate_all else {self.MGMT_LABEL}

        # Collect interfaces that match by label
        target_ifaces = []
        for iface in interfaces:
            label = self._normalize_label(getattr(iface, "label", None))
            if label in target_labels:
                target_ifaces.append(iface)

        if not target_ifaces:
            raise AbortScript(
                "No interfaces matched the required labels. "
                "Ensure Interface.label on the device matches one of: "
                f"{', '.join(sorted(target_labels))}"
            )

        mgmt_ip_obj = None

        for iface in target_ifaces:
            label = self._normalize_label(iface.label)
            prefix = self._find_prefix(site, label, allow_global_fallback)

            ip_obj = self._assign_ip_to_interface(iface, prefix, tenant=tenant)
            self.log_success(
                f"Assigned {ip_obj.address} to {device.name} / {iface.name} (label={label}, prefix={prefix.prefix})"
            )

            if label == self.MGMT_LABEL:
                mgmt_ip_obj = ip_obj

        if mgmt_ip_obj:
            device.primary_ip4 = mgmt_ip_obj
            device.save()
            self.log_success(f"Set primary IPv4 for {device.name} to {mgmt_ip_obj.address}")
        else:
            self.log_warning(f"No '{self.MGMT_LABEL}' interface allocated; primary IPv4 not set.")

        # ---- Optional cabling ----
        if make_peer:
            self._connect_device_to_peer_by_name(device, b_device)

        return f"Commissioning complete for {device.name}."
