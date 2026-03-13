from extras.scripts import (
    Script,
    StringVar,
    ObjectVar,
    ChoiceVar,
    BooleanVar,
    TextVar,
    IntegerVar,
)
from utilities.exceptions import AbortScript

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.core.exceptions import ValidationError

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Platform,
    Site,
    Interface,
    Cable,
    Rack,
    Region,
)
from dcim.choices import DeviceStatusChoices # RackFaceChoices
from dcim.models.cables import CableTermination

from ipam.models import IPAddress, Prefix
from ipam.choices import IPAddressStatusChoices

from tenancy.models import Tenant

import ipaddress
import re


class CommissionDevice(Script):
    class Meta:
        name = "Commission New Device (v4.4.5) – Create, Address, Cable"
        description = (
            "Creates a device (interfaces from DeviceType templates), optionally racks it, "
            "allocates IPs based on interface labels and site-tagged prefixes, "
            "allocates /31 subnets for selected uplink labels, sets oob-mgmt as primary IPv4, "
            "and optionally creates cables from a patch plan mapping. All operations are atomic; "
            "any error triggers rollback."
        )
        field_order = (
            "region",
            "site",
            "tenant",
            "device_id",
            "device_type",
            "role",
            "platform",
            "status",
            "rack",
            # "rack_face",
            "rack_position",
            "allocate_all_labeled_interfaces",
            "create_cables_from_patch_plan",
            "enforce_patch_plan_site_match",
            "patch_plan",
        )

    #
    # --- Form Fields ---
    #

    region = ObjectVar(
        model=Region,
        label="Region",
        required=True,
    )

    site = ObjectVar(
        model=Site,
        label="Site (filtered by Region)",
        required=True,
        query_params={"region_id": "$region"},
    )

    # Tenant is now REQUIRED (used in device name)
    tenant = ObjectVar(
        model=Tenant,
        label="Tenant",
        required=True,
    )

    # Removed hostname; device name is computed
    device_id = StringVar(
        label="Device ID (stored as asset_tag and used in the computed name)",
        required=True,
    )

    device_type = ObjectVar(
        model=DeviceType,
        label="Device Type",
        required=True,
    )

    role = ObjectVar(
        model=DeviceRole,
        label="Device Role",
        required=True,
    )

    platform = ObjectVar(
        model=Platform,
        label="Platform (optional)",
        required=False,
    )

    status = ChoiceVar(
        label="Device Status",
        choices=DeviceStatusChoices,
        default=DeviceStatusChoices.STATUS_STAGED,
        required=True,
    )

    # Rack placement
    rack = ObjectVar(
        model=Rack,
        label="Rack (optional; filtered by Site)",
        required=False,
        query_params={"site_id": "$site"},
    )

    # rack_face = ChoiceVar(
    #     label="Rack Face",
    #     choices=RackFaceChoices,
    #     default=RackFaceChoices.FACE_FRONT,
    #     required=True,
    # )

    rack_position = IntegerVar(
        label="Rack Position (U; optional)",
        required=False,
        description=(
            "If provided, it must be available (and contiguous for device height). "
            "If left blank, the script will auto-place at the first valid contiguous space."
        ),
    )

    # Addressing & Cabling toggles
    allocate_all_labeled_interfaces = BooleanVar(
        label="Allocate IPs for all relevant labeled interfaces",
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
            "  Ethernet1/1=switch-1:Ethernet1/1\n"
            "  Ethernet1/10=switch-2:Ethernet1/10\n"
        ),
    )

    #
    # --- Label/Tag Definitions ---
    #

    # Interfaces with these labels get a single IP from a site-scoped prefix tagged with the same name
    SINGLE_IP_LABELS = {
        "oob-mgmt",
        "underlay-loopback1",
        "underlay-loopback2",
    }

    # Interfaces with these labels get a /31 child prefix from a site-scoped prefix tagged with the same name;
    # the first IP of that /31 is assigned locally.
    SUBNET_31_LABELS = {
        "spine-a-uplink",
        "spine-b-uplink",
        "vpc-peerlink",
    }

    MGMT_LABEL = "oob-mgmt"

    #
    # --- Helpers ---
    #

    def _normalize(self, s: str) -> str:
        return (s or "").strip()

    def _slug(self, s: str) -> str:
        """
        Make a safe segment: lowercase, spaces/underscores -> '-', remove illegal chars, collapse dashes.
        """
        s = (s or "").strip().lower()
        s = re.sub(r"[ _]+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        return s or "na"

    def _compose_device_name(self, site: Site, tenant: Tenant, role: DeviceRole, device_id: str) -> str:
        site_part = self._slug(site.name)
        tenant_part = self._slug(tenant.name)
        role_part = self._slug(role.name)
        id_part = self._slug(device_id)
        return f"{site_part}-{tenant_part}-{role_part}-{id_part}"

    def _find_site_prefix_by_tag(self, site: Site, tag_name: str) -> Prefix:
        qs = Prefix.objects.filter(site=site, tags__name=tag_name)
        if not qs.exists():
            raise AbortScript(
                f"No prefix found for site='{site}' with tag='{tag_name}'. "
                "Ensure Prefix.site is set and the Prefix is tagged correctly."
            )
        # Prefer the most specific (longest) prefix
        candidates = list(qs)
        candidates.sort(key=lambda p: (p.prefix.prefixlen, str(p.prefix)), reverse=True)
        return candidates[0]

    def _allocate_next_ip(self, prefix: Prefix) -> str:
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        return str(ip)

    def _allocate_next_child_31(self, parent: Prefix) -> Prefix:
        """
        Allocate the next available /31 child Prefix under `parent`.
        Returns the newly-created child Prefix object.
        """
        child_cidr = parent.get_first_available_prefix(prefix_length=31)
        if not child_cidr:
            raise AbortScript(f"No available /31 child prefixes left in {parent.prefix}.")

        child = Prefix(
            prefix=str(child_cidr),
            site=parent.site,
            vrf=parent.vrf,
            tenant=parent.tenant,
            status=parent.status,
            role=parent.role,
            is_pool=False,
            description=f"Allocated by Script under {parent.prefix} (tag-matched)",
        )
        child.full_clean()
        child.save()
        return child

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

    def _is_interface_cabled(self, iface: Interface) -> bool:
        """
        Return True if the interface already has a cable attached.
        NetBox 4.x requires querying CableTermination via GenericFK fields.
        """
        ct = ContentType.objects.get_for_model(iface.__class__)
        return CableTermination.objects.filter(
            termination_type=ct,
            termination_id=iface.pk,
        ).exists()

    def _create_cable(self, a_iface: Interface, b_iface: Interface) -> None:
        """
        Create cable between A and B. Any in-use port causes Abort/rollback.
        """
        if self._is_interface_cabled(a_iface):
            raise AbortScript(
                f"A-side port already in use: {a_iface.device.name}:{a_iface.name}"
            )
        if self._is_interface_cabled(b_iface):
            raise AbortScript(
                f"B-side port already in use: {b_iface.device.name}:{b_iface.name}"
            )

        Cable.objects.create(
            a_terminations=[a_iface],
            b_terminations=[b_iface],
            status="connected",
        )
        self.log_success(
            f"Cabled: {a_iface.device.name}:{a_iface.name} <-> {b_iface.device.name}:{b_iface.name}"
        )

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

    def _auto_place_in_rack(self, device: Device, rack: Rack, face: str) -> None:
        """
        Attempt to place `device` into `rack` automatically by probing valid positions.
        This doesn't rely on Rack.get_available_units(); it tries positions top-down until
        device.full_clean() passes. Raises AbortScript if no contiguous space is found.
        """
        height = device.device_type.u_height or 1
        for pos in range(rack.u_height, 0, -1):
            device.rack = rack
            device.face = face
            device.position = pos
            try:
                device.full_clean()
                device.save()
                self.log_success(f"Auto-placed device at {rack} face={face} U={pos} (height={height}U)")
                return
            except ValidationError:
                continue

        raise AbortScript(
            f"Could not auto-place device in rack '{rack}'. No contiguous {height}U space available."
        )

    #
    # --- Main ---
    #

    def run(self, data, commit):
        region = data["region"]
        site = data["site"]
        tenant = data["tenant"]  # now required
        device_type = data["device_type"]
        role = data["role"]
        platform = data.get("platform")
        status = data["status"]

        rack = data.get("rack")
        # rack_face = data["rack_face"]
        rack_position = data.get("rack_position")

        device_id = self._normalize(data["device_id"])

        allocate_all = data["allocate_all_labeled_interfaces"]
        do_cabling = data["create_cables_from_patch_plan"]
        enforce_site_match = data["enforce_patch_plan_site_match"]
        patch_plan_text = data.get("patch_plan")

        # Pre-flight checks
        if site.region_id != region.id:
            raise AbortScript(
                f"Selected site '{site}' does not belong to region '{region}'."
            )

        # Compute device name
        composed_name = self._compose_device_name(site, tenant, role, device_id)

        if Device.objects.filter(name=composed_name).exists():
            raise AbortScript(f"Device name '{composed_name}' already exists in NetBox.")

        # Atomic transaction - any AbortScript or exception will roll back all DB changes
        with transaction.atomic():
            if not commit:
                self.log_info(
                    f"[DRY-RUN] Would create device '{composed_name}', rack/place it, allocate IPs (/32 & /31), and optionally create cables."
                )
                return "Dry-run complete."

            # Create device (interfaces from DeviceType templates are instantiated on save)
            device = Device(
                name=composed_name,
                site=site,
                tenant=tenant,
                device_type=device_type,
                role=role,
                platform=platform,
                status=status,
                asset_tag=device_id,  # keep device_id as asset_tag as well
            )
            device.full_clean()
            device.save()
            self.log_success(
                f"Created device: {device.name} (site={site}, type={device_type}, role={role}, asset_tag={device_id})"
            )

            # Optional rack placement
            if rack:
                if rack.site_id != site.id:
                    raise AbortScript(
                        f"Selected Rack '{rack}' does not belong to Site '{site}'."
                    )

                device.rack = rack
                # device.face = rack_face

                if rack_position:
                    device.position = rack_position
                    # Validate explicit placement
                    device.full_clean()
                    device.save()
                    # self.log_success(
                    #     f"Placed device in rack {rack} face={rack_face} U={rack_position}"
                    # )
                    self.log_success(
                        f"Placed device in rack {rack}  U={rack_position}"
                     )
                else:
                    # Try to auto-place by probing available positions
                    self._auto_place_in_rack(device, rack)

            # Discover newly-instantiated interfaces
            interfaces = list(device.interfaces.all())
            if not interfaces:
                raise AbortScript(
                    "No interfaces found on device after creation. "
                    "Check that the Device Type has interface templates defined."
                )

            single_ip_labels = set(self.SINGLE_IP_LABELS)
            subnet_31_labels = set(self.SUBNET_31_LABELS)

            ifaces_processed = 0
            mgmt_ip_obj = None

            for iface in interfaces:
                label = self._normalize(getattr(iface, "label", None))
                if not label:
                    # Ignore unlabeled interfaces
                    continue

                # /32 IP allocation for label-tag-matched prefixes
                if label in single_ip_labels:
                    parent = self._find_site_prefix_by_tag(site, label)
                    addr = self._allocate_next_ip(parent)

                    ip_obj = IPAddress(
                        address=addr,
                        vrf=parent.vrf,
                        tenant=tenant or parent.tenant,
                        status=IPAddressStatusChoices.STATUS_ACTIVE,
                        assigned_object=iface,
                    )
                    ip_obj.full_clean()
                    ip_obj.save()

                    self.log_success(
                        f"Assigned {ip_obj.address} to {device.name}/{iface.name} (label={label}, parent={parent.prefix})"
                    )

                    if label == self.MGMT_LABEL:
                        mgmt_ip_obj = ip_obj

                    ifaces_processed += 1
                    continue

                # /31 allocation for label-tag-matched uplinks
                if label in subnet_31_labels:
                    parent = self._find_site_prefix_by_tag(site, label)
                    child = self._allocate_next_child_31(parent)

                    # First usable IP from the /31 (network + 1)
                    net = ipaddress.ip_network(str(child.prefix))
                    hosts = list(net.hosts())
                    if len(hosts) < 1:
                        raise AbortScript(f"Allocated /31 {child.prefix} has no usable host IPs.")

                    first_ip = str(hosts[0])

                    ip_obj = IPAddress(
                        address=first_ip,
                        vrf=child.vrf,
                        tenant=tenant or child.tenant,
                        status=IPAddressStatusChoices.STATUS_ACTIVE,
                        assigned_object=iface,
                    )
                    ip_obj.full_clean()
                    ip_obj.save()

                    self.log_success(
                        f"Allocated {child.prefix} under {parent.prefix} and assigned {first_ip} to {device.name}/{iface.name} (label={label})"
                    )

                    ifaces_processed += 1
                    continue

                # Not a target label -> ignore
                continue

            # Primary IPv4 = oob-mgmt IP
            if mgmt_ip_obj:
                device.primary_ip4 = mgmt_ip_obj
                device.save()
                self.log_success(f"Set primary IPv4 for {device.name} to {mgmt_ip_obj.address}")
            else:
                self.log_warning(
                    f"No '{self.MGMT_LABEL}' interface allocated; primary IPv4 not set."
                )

            if ifaces_processed == 0:
                self.log_warning(
                    "No interfaces matched the requested label policies. "
                    "Labels expected for allocation: "
                    f"{', '.join(sorted(single_ip_labels | subnet_31_labels))}"
                )

            # Optional cabling via patch plan
            if do_cabling:
                mappings = self._parse_patch_plan(patch_plan_text)
                if not mappings:
                    raise AbortScript("Cabling enabled but patch plan is empty. Add at least one mapping line.")

                a_if_by_name = {i.name: i for i in device.interfaces.all()}

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

                    a_label = self._normalize(getattr(a_iface, "label", None))

                    # Update descriptions/enabled flags on both sides (before cabling)
                    self._set_iface_desc_and_enable(
                        iface=b_iface,
                        device_name=a_iface.device.name,
                        port_name=a_iface.name,
                        label=a_label,
                    )
                    self._set_iface_desc_and_enable(
                        iface=a_iface,
                        device_name=b_iface.device.name,
                        port_name=b_iface.name,
                        label=a_label,
                    )

                    # Create cable (raises if any side in-use)
                    self._create_cable(a_iface, b_iface)

            return f"Commissioning complete for {device.name}."
