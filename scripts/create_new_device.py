# File: netbox/scripts/commission_device.py
#
# NetBox v4.4.5 Custom Script:
# - Create a new device (interfaces instantiated from DeviceType interface templates)
# - Allocate the next available IP from a site-scoped Prefix whose Tag name matches the interface label
#   (idn-mgmt, idn-dmz-a, idn-dmz-b, idn-rtp)
# - Set idn-mgmt IP as the device primary IPv4
# - Optionally create cables based on a patch plan mapping:
#     eth0=switch-1:Ethernet1/1
#     eth1=switch-2:Ethernet1/10

from extras.scripts import Script, StringVar, ObjectVar, ChoiceVar, BooleanVar, TextVar
from utilities.exceptions import AbortScript

from dcim.models import Device, DeviceRole, DeviceType, Platform, Site, Interface, Cable
from dcim.choices import DeviceStatusChoices

from ipam.models import IPAddress, Prefix
from ipam.choices import IPAddressStatusChoices
from django.contrib.contenttypes.models import ContentType
from dcim.models.cables import CableTermination



from tenancy.models import Tenant


class CommissionDevice(Script):
    class Meta:
        name = "Commission New Device (IP Allocation + Patch Plan Cabling)"
        description = (
            "Creates a device, allocates IPs from site/tag-matched prefixes based on interface labels, "
            "sets idn-mgmt as primary IPv4, and optionally cables interfaces using a patch plan mapping."
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
            "create_cables_from_patch_plan",
            "enforce_patch_plan_site_match",
            "patch_plan",
        )

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

    # NetBox 4.4.x: Device.role is required, so make it required here too.
    role = ObjectVar(
        model=DeviceRole,
        label="Device Role",
        required=True,
    )

    tenant = ObjectVar(
        model=Tenant,
        label="Tenant (optional)",
        required=False,
    )

    status = ChoiceVar(
        label="Device Status",
        choices=DeviceStatusChoices,
        default=DeviceStatusChoices.STATUS_STAGED,
        required=True,
    )

    allocate_all_labeled_interfaces = BooleanVar(
        label="Allocate IPs for all labeled interfaces",
        default=True,
        required=True,
    )

    create_cables_from_patch_plan = BooleanVar(
        label="Create cables from patch plan",
        default=False,
        required=True,
    )

    enforce_patch_plan_site_match = BooleanVar(
        label="Enforce B-side devices are in selected site",
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
            "  eth1=switch-2:Ethernet1/10\n"
        ),
    )

    # Updated tags/labels
    SUBNET_LABELS = (
        "idn-mgmt",
        "idn-dmz-a",
        "idn-dmz-b",
        "idn-rtp",
    )
    MGMT_LABEL = "idn-mgmt"

    def _normalize(self, s: str) -> str:
        return (s or "").strip()

    def _find_site_prefix_by_tag(self, site: Site, tag_name: str) -> Prefix:
        qs = Prefix.objects.filter(site=site, tags__name=tag_name)
        if not qs.exists():
            raise AbortScript(
                f"No prefix found for site='{site}' with tag='{tag_name}'. "
                "Ensure Prefix.site is set and the Prefix is tagged correctly."
            )

        candidates = list(qs)
        candidates.sort(key=lambda p: (p.prefix.prefixlen, str(p.prefix)), reverse=True)
        return candidates[0]

    def _allocate_next_ip(self, prefix: Prefix) -> str:
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        return str(ip)

    def _parse_patch_plan(self, text: str):
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

    def _create_cable(self, a_iface: Interface, b_iface: Interface) -> bool:
        if getattr(b_iface, "cable", None):
            self.log_warning(
                f"Skipping (B-Side switch port is already in use): {b_iface.device.name}:{b_iface.name}"
            )
            return False

        Cable.objects.create(
            a_terminations=[a_iface],
            b_terminations=[b_iface],
            status="connected",
        )
        # a_device = a_iface.device.name
        # a_port = a_iface.name
        # a_label = (a_iface.label or "").strip()

        # desc = f"{a_device}-{a_port}-{a_label}"

        # b_iface.description = desc
        # b_iface.full_clean()
        # b_iface.save()
        self.log_success(
            f"Cabled: {a_iface.device.name}:{a_iface.name} <-> {b_iface.device.name}:{b_iface.name}"
        )
        return True


    def _set_iface_desc_and_enable(self, iface: Interface, device_name: str, port_name: str, label: str):
        """
        Set interface enabled=True and description to: <device_name>-<port_name>-<label>
        """
        label = (label or "").strip()
        desc = f"{device_name}-{port_name}-{label}"
    
        iface.description = desc
        iface.enabled = True
        iface.full_clean()
        iface.save()
    
        self.log_success(
            f"Updated {iface.device.name}:{iface.name} (enabled=True, description='{desc}')"
        )


    def run(self, data, commit):
        hostname = self._normalize(data["hostname"])
        site = data["site"]
        platform = data["platform"]
        device_type = data["device_type"]
        role = data["role"]  # required now
        tenant = data.get("tenant")
        status = data["status"]

        allocate_all = data["allocate_all_labeled_interfaces"]
        do_cabling = data["create_cables_from_patch_plan"]
        enforce_site_match = data["enforce_patch_plan_site_match"]
        patch_plan_text = data.get("patch_plan")

        if Device.objects.filter(name=hostname).exists():
            raise AbortScript(f"Device name '{hostname}' already exists in NetBox.")

        if not commit:
            self.log_info("[DRY-RUN] Would create device, allocate IPs, and optionally create cables.")
            return "Dry-run complete."

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

        interfaces = list(device.interfaces.all())
        if not interfaces:
            raise AbortScript(
                "No interfaces found on device after creation. "
                "Check that the Device Type has interface templates defined."
            )

        target_labels = set(self.SUBNET_LABELS) if allocate_all else {self.MGMT_LABEL}

        target_ifaces = []
        for iface in interfaces:
            if getattr(iface, "label", None) and iface.label.strip() in target_labels:
                target_ifaces.append(iface)

        if not target_ifaces:
            raise AbortScript(
                "No interfaces matched the required labels. "
                "Ensure Interface.label matches one of: "
                f"{', '.join(sorted(target_labels))}"
            )

        mgmt_ip_obj = None

        for iface in target_ifaces:
            label = iface.label.strip()
            prefix = self._find_site_prefix_by_tag(site, label)
            addr = self._allocate_next_ip(prefix)

            ip_obj = IPAddress(
                address=addr,
                vrf=prefix.vrf,
                tenant=tenant or prefix.tenant,
                status=IPAddressStatusChoices.STATUS_ACTIVE,
                assigned_object=iface,
            )
            ip_obj.full_clean()
            ip_obj.save()

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

        if do_cabling:
            mappings = self._parse_patch_plan(patch_plan_text)
            if not mappings:
                raise AbortScript("Cabling enabled but patch plan is empty. Add at least one mapping line.")

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

                b_dev = Device.objects.filter(name=b_dev_name).first()
                if not b_dev:
                    raise AbortScript(f"B-side device '{b_dev_name}' not found in NetBox.")

                if enforce_site_match and b_dev.site_id != site.id:
                    raise AbortScript(
                        f"B-side device '{b_dev.name}' is in site '{b_dev.site}', not '{site}'."
                    )

                b_iface = Interface.objects.filter(device=b_dev, name=b_if_name).first()
                if not b_iface:
                    raise AbortScript(
                        f"B-side interface '{b_if_name}' not found on device '{b_dev.name}'. "
                        "Check interface name matches exactly in NetBox."
                    )
                a_label = (a_iface.label or "").strip() 
                # B-side desc = A-device, A-port, A-label
                self._set_iface_desc_and_enable(
                    iface=b_iface,
                    device_name=a_iface.device.name,
                    port_name=a_iface.name,
                    label=a_label,
                )
            
                # A-side desc = B-device, B-port, A-label
                self._set_iface_desc_and_enable(
                    iface=a_iface,
                    device_name=b_iface.device.name,
                    port_name=b_iface.name,
                    label=a_label,
                )
                           
                if self._create_cable(a_iface, b_iface):
                    created += 1
                else:
                    skipped += 1
                    

            self.log_info(f"Patch plan cabling summary: created={created}, skipped={skipped}")

        return f"Commissioning complete for {device.name}."
