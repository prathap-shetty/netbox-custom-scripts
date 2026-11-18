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
    ModuleBay,
)
from dcim.choices import DeviceStatusChoices # RackFaceChoices
from dcim.models.cables import CableTermination

from ipam.models import IPAddress, Prefix
from ipam.choices import IPAddressStatusChoices

from tenancy.models import Tenant

import ipaddress
import netaddr
from decimal import Decimal, InvalidOperation
import re
import os
import pynetbox

RACK_FACE_CHOICES = (
    ("front", "Front"),
    ("rear", "Rear"),
 )
NETWORK_TYPE_CHOICES = (
    ("pod1", "pod1"),
    ("pod2", "pod2"),
    ("pod3", "pod3"),
    ("azA", "azA"),
    ("azB", "azB"),
 )

# Device-type-specific bay plans
# Keys match DeviceType.model exactly (case sensitive as stored in NetBox)
MODULE_BAY_PLANS = {
    "N9K-C9364D-GX2A": {
        "name_prefix": "QSFP-DD - port ",
        "start": 1,
        "end": 64,   # inclusive
        # optional: "position_format": "{i}"  # defaults to "{i}"
    },
    # Add more models here as needed
}


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
            "network_type",
            "device_id",
            "device_type",
            "role",
            "platform",
            "status",
            "rack",
            "rack_face",
            "rack_position",
            "allocate_all_labeled_interfaces",
            "align_host_octet",
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

    network_type = ChoiceVar(
        choices=NETWORK_TYPE_CHOICES,
        label="Network Type",
        required=True,
    )

    # Removed hostname; device name is computed
    device_id = StringVar(
        label="Device ID(s) (Used in the computed name)",
        required=True,
        description=(
            "Single ID or comma-separated list, e.g. 1,10,20,125. "
            "One device is created per ID with the same settings."
        ),
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
        description="Default rack. Per-device racks can be set in Rack Position(s) as RACK:U.",
        query_params={"site_id": "$site"},
    )

    rack_face = ChoiceVar(
        label="Rack Face",
        choices=RACK_FACE_CHOICES,
        default="front",
        required=True,
    )

    rack_position = StringVar(
        label="Rack Position(s) (U; optional)",
        required=False,
        description=(
            "One entry per Device ID, comma-separated in the same order. Each entry is one of: "
            "'40' (U40 in the default Rack), 'RACK02:40' (U40 in rack RACK02), "
            "'RACK02:' (auto-place in RACK02), or blank (auto-place in the default Rack, "
            "or no rack if none is selected). Example: RACK01:40,RACK02:40,,RACK03:. "
            "Leave the whole field blank to auto-place all in the default Rack."
        ),
    )

    # Addressing & Cabling toggles
    allocate_all_labeled_interfaces = BooleanVar(
        label="Allocate IPs for all relevant labeled interfaces",
        default=True,
        required=True,
    )

    align_host_octet = BooleanVar(
        label="Use the same last octet for mgmt and loopback IPs",
        default=True,
        required=False,
        description=(
            "Give oob-mgmt and loopback0/1/2 the same host part, e.g. mgmt x.x.x.10 -> "
            "loopback0 y.y.y.10. Uses the lowest host number that is free in all of those "
            "prefixes. Untick to take the next free IP in each prefix independently."
        ),
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
            "  <DEVICE_ID>|<A_INTERFACE>=<B_DEVICE>:<B_INTERFACE>\n"
            "The '<DEVICE_ID>|' prefix selects which new device is the A-side. "
            "It may be omitted only when a single Device ID is given.\n\n"
            "Examples:\n"
            "  10|Ethernet1/1=switch-1:Ethernet1/1\n"
            "  20|Ethernet1/1=switch-1:Ethernet1/2\n"
        ),
    )

    #
    # --- Label/Tag Definitions ---
    #

    # Interfaces with these labels get a single IP from a site-scoped prefix tagged with the same name
    SINGLE_IP_LABELS = {
        "oob-mgmt",
        "loopback0",
        "loopback1",
        "loopback2",
    }

    # Interfaces with these labels get a /31 child prefix from a site-scoped prefix tagged with the same name;
    # the first IP of that /31 is assigned locally.
    SUBNET_31_LABELS = {
        "spine-a-uplink",
        "spine-b-uplink",
        "vpc-peerlink",
    }

    MGMT_LABEL = "oob-mgmt"

    # Labels whose IP is stored as a host route (/32) instead of the parent prefix length
    HOST_ROUTE_LABELS = {
        "loopback0",
        "loopback1",
        "loopback2",
    }

    #
    # --- Helpers ---
    #
    def _create_module_bays_for_device(self, device: Device) -> int:
        """
        Create module bays on a device based on its DeviceType.model using MODULE_BAY_PLANS.
        Idempotent: existing bays with the same name are skipped.
        Returns the number of bays created.
        """
        plan = MODULE_BAY_PLANS.get(device.device_type.model)
        if not plan:
            self.log_info(f"No module-bay plan for device type '{device.device_type.model}'; skipping.")
            return 0
    
        created = 0
        prefix = plan["name_prefix"]
        start = int(plan["start"])
        end = int(plan["end"])
        pos_fmt = plan.get("position_format", "{i}")
    
        # Build a set of existing names for fast lookups
        existing = set(device.modulebays.values_list("name", flat=True))
    
        for i in range(start, end + 1):
            name = f"{prefix}{i}"
            if name in existing:
                continue
    
            mb = ModuleBay(
                device=device,
                name=name,
                position=pos_fmt.format(i=i),  # numeric/string OK
                # label=name,  # uncomment if you also want label filled
            )
            mb.full_clean()
            mb.save()
            created += 1
    
        if created:
            self.log_success(f"Created {created} module bays on {device.name} (type={device.device_type.model}).")
        else:
            self.log_info(f"All module bays for plan already exist on {device.name}; nothing to create.")
    
        return created

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

    def _compose_device_name(self, site: Site, tenant: Tenant, network_type: str, role: DeviceRole, device_id: str) -> str:
        site_part = self._slug(site.name)
        tenant_part = self._slug(tenant.name)
        network_type_part = self._slug(network_type)
        role_part = self._slug(role.name)
        id_part = self._slug(device_id)
        return f"{site_part}-{network_type_part}-{role_part}-{id_part}"

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

    def _find_common_host_offset(self, prefixes):
        """
        Return the lowest host offset (IP minus network address) that is available in
        every prefix, or None. For /24 prefixes the offset equals the last octet.
        """
        available = [(p, p.get_available_ips()) for p in prefixes]
        smallest = min(p.prefix.size for p in prefixes)
        for offset in range(smallest):
            if all(
                netaddr.IPAddress(p.prefix.first + offset, p.prefix.version) in avail
                for p, avail in available
            ):
                return offset
        return None

    def _ip_at_offset(self, prefix: Prefix, offset: int) -> str:
        ip = netaddr.IPAddress(prefix.prefix.first + offset, prefix.prefix.version)
        return f"{ip}/{prefix.prefix.prefixlen}"

    def _allocate_next_ip(self, prefix: Prefix) -> str:
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        return str(ip)

    # def _allocate_next_child_31(self, parent: Prefix) -> Prefix:
    #     """
    #     Allocate the next available /31 child Prefix under `parent`.
    #     Returns the newly-created child Prefix object.
    #     """
    #     child_cidr = parent.get_first_available_prefix(31)
    #     if not child_cidr:
    #         raise AbortScript(f"No available /31 child prefixes left in {parent.prefix}.")

    #     child = Prefix(
    #         prefix=str(child_cidr),
    #         site=parent.site,
    #         vrf=parent.vrf,
    #         tenant=parent.tenant,
    #         status=parent.status,
    #         role=parent.role,
    #         is_pool=False,
    #         description=f"Allocated by Script under {parent.prefix} (tag-matched)",
    #     )
    #     child.full_clean()
    #     child.save()
    #     return child
 
    def _safe_id(self, val):
        if val and hasattr(val, "id"):
            return val.id
        return None
    
    def _allocate_next_child_31(self, parent: Prefix):
        import os
        import pynetbox
    
        nb = pynetbox.api(
            os.getenv("NETBOX_URL", "https://192.168.0.105:8443"),
            token=os.getenv("NETBOX_TOKEN", "ba5478e72306fa893c9671382479ce52b49b9790"),
        )
        nb.http_session.verify = False
    
        parent_api = nb.ipam.prefixes.get(parent.id)
        if not parent_api:
            raise AbortScript(f"Unable to retrieve parent prefix {parent.prefix} ({parent.id})")
    
        payload = {
            "prefix_length": 31,
            "description": f"Allocated under {parent.prefix}",
            "site": self._safe_id(parent.site),
            "vrf": self._safe_id(parent.vrf),
            "tenant": self._safe_id(parent.tenant),
        }
    
        new = parent_api.available_prefixes.create(payload)
        if not new:
            raise AbortScript(f"No /31 prefixes available under {parent.prefix}")
    
        return Prefix.objects.get(id=new.id)    

    def _parse_device_ids(self, text: str):
        """
        Split a comma/whitespace-separated list of device IDs.
        Order is preserved; duplicates abort the run.
        """
        ids = [self._normalize(p) for p in re.split(r"[,\s]+", text or "")]
        ids = [i for i in ids if i]
        if not ids:
            raise AbortScript("Device ID is empty. Provide one ID or a comma-separated list.")

        seen = set()
        for i in ids:
            if i in seen:
                raise AbortScript(f"Device ID '{i}' is listed more than once.")
            seen.add(i)
        return ids

    def _parse_patch_plan(self, text: str, device_ids):
        """
        Parse patch plan lines into {device_id: [(a_iface, b_device, b_iface), ...]}.
        Lines may be prefixed with '<DEVICE_ID>|'. Unprefixed lines are only allowed
        when exactly one device ID is being created.
        """
        mappings = {did: [] for did in device_ids}
        if not text:
            return mappings

        for idx, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if "|" in line:
                did, line = line.split("|", 1)
                did = self._normalize(did)
                line = line.strip()
                if did not in mappings:
                    raise AbortScript(
                        f"Patch plan line {idx} references device ID '{did}', "
                        f"which is not in the Device ID list ({', '.join(device_ids)})."
                    )
            elif len(device_ids) == 1:
                did = device_ids[0]
            else:
                raise AbortScript(
                    f"Patch plan line {idx} has no '<DEVICE_ID>|' prefix: '{raw}'. "
                    "A prefix is required when creating multiple devices."
                )

            if "=" not in line or ":" not in line:
                raise AbortScript(
                    f"Patch plan line {idx} is invalid: '{raw}'. "
                    "Expected format: [<DEVICE_ID>|]<A_INTERFACE>=<B_DEVICE>:<B_INTERFACE>"
                )

            left, right = line.split("=", 1)
            b_dev, b_if = right.split(":", 1)

            a_iface = self._normalize(left)
            b_device = self._normalize(b_dev)
            b_iface = self._normalize(b_if)

            if not a_iface or not b_device or not b_iface:
                raise AbortScript(f"Patch plan line {idx} has empty values: '{raw}'")

            mappings[did].append((a_iface, b_device, b_iface))

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

    def _parse_rack_positions(self, text: str, device_ids, site: Site, default_rack):
        """
        Parse comma-separated placement entries into {device_id: (rack or None, Decimal or None)}.
        Entry forms: '' | 'U' | 'RACK:U' | 'RACK:'. Racks are looked up by name in `site`.
        Blank field -> every device goes to (default_rack, auto).
        """
        placements = {did: (default_rack, None) for did in device_ids}
        text = (text or "").strip()
        if not text:
            return placements

        parts = [p.strip() for p in text.split(",")]
        if len(parts) != len(device_ids):
            raise AbortScript(
                f"Rack Position has {len(parts)} entries but {len(device_ids)} Device ID(s) were given. "
                "Provide one entry per Device ID (blank entry = auto-place)."
            )

        rack_cache = {}
        seen = {}
        for did, part in zip(device_ids, parts):
            rack, pos_text = default_rack, part
            if ":" in part:
                rack_name, pos_text = (x.strip() for x in part.rsplit(":", 1))
                if rack_name not in rack_cache:
                    matches = list(Rack.objects.filter(site=site, name=rack_name))
                    if not matches:
                        raise AbortScript(f"Rack '{rack_name}' (device ID '{did}') not found in site '{site}'.")
                    if len(matches) > 1:
                        raise AbortScript(
                            f"Rack name '{rack_name}' (device ID '{did}') matches {len(matches)} racks in "
                            f"site '{site}'; rack names must be unique within the site to use RACK:U."
                        )
                    rack_cache[rack_name] = matches[0]
                rack = rack_cache[rack_name]

            if not pos_text:
                placements[did] = (rack, None)
                continue

            try:
                pos = Decimal(pos_text)
            except InvalidOperation:
                raise AbortScript(
                    f"Rack Position '{part}' for device ID '{did}' is invalid. "
                    "Use U, RACK:U, RACK: or leave blank."
                )
            if pos <= 0 or pos % Decimal("0.5") != 0:
                raise AbortScript(
                    f"Rack Position '{part}' for device ID '{did}' must be a positive whole or half U."
                )
            if rack is None:
                raise AbortScript(
                    f"Rack Position '{part}' for device ID '{did}' has no rack. "
                    "Select a default Rack or use RACK:U."
                )
            key = (rack.pk, pos)
            if key in seen:
                raise AbortScript(
                    f"{rack} U{pos_text} is given for both device ID '{seen[key]}' and '{did}'."
                )
            seen[key] = did
            placements[did] = (rack, pos)
        return placements

    def _auto_place_in_rack(self, device: Device, rack: Rack, face: str, reserved=()) -> None:
        """
        Attempt to place `device` into `rack` automatically by probing valid positions.
        This doesn't rely on Rack.get_available_units(); it tries positions top-down until
        device.full_clean() passes. Raises AbortScript if no contiguous space is found.
        `reserved` holds U positions claimed by devices later in this run; they are skipped.
        """
        height = device.device_type.u_height or 1
        for pos in range(rack.u_height, 0, -1):
            if any(pos <= r < pos + height for r in reserved):
                continue
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
        network_type = data["network_type"]
        rack = data.get("rack")
        rack_position = data.get("rack_position")

        device_ids = self._parse_device_ids(data["device_id"])

        do_cabling = data["create_cables_from_patch_plan"]
        patch_plan_text = data.get("patch_plan")

        # Pre-flight checks
        if site.region_id != region.id:
            raise AbortScript(
                f"Selected site '{site}' does not belong to region '{region}'."
            )

        if rack and rack.site_id != site.id:
            raise AbortScript(
                f"Selected Rack '{rack}' does not belong to Site '{site}'."
            )

        placements = self._parse_rack_positions(rack_position, device_ids, site, rack)

        # (rack, U) units claimed by explicit positions, so auto-placed devices don't take them
        height = device_type.u_height or 1
        units_by_id = {
            did: [(r.pk, pos + i) for i in range(int(height) or 1)]
            for did, (r, pos) in placements.items()
            if pos is not None
        }

        # Compute all device names up front and reject any that already exist
        names = {
            did: self._compose_device_name(site, tenant, network_type, role, did)
            for did in device_ids
        }
        existing = list(
            Device.objects.filter(name__in=names.values()).values_list("name", flat=True)
        )
        if existing:
            raise AbortScript(f"Device name(s) already exist in NetBox: {', '.join(existing)}")

        # Parse patch plan once, grouped by device ID
        mappings_by_id = {did: [] for did in device_ids}
        if do_cabling:
            mappings_by_id = self._parse_patch_plan(patch_plan_text, device_ids)
            if not any(mappings_by_id.values()):
                raise AbortScript("Cabling enabled but patch plan is empty. Add at least one mapping line.")
            for did, maps in mappings_by_id.items():
                if not maps:
                    self.log_warning(f"No patch plan lines for device ID '{did}' ({names[did]}); no cables for it.")

        # Atomic transaction - any AbortScript or exception will roll back all DB changes
        with transaction.atomic():
            if not commit:
                for did in device_ids:
                    p_rack, p_pos = placements[did]
                    placement = (
                        "no rack" if not p_rack
                        else f"{p_rack} U={p_pos}" if p_pos is not None
                        else f"{p_rack} auto-place"
                    )
                    self.log_info(
                        f"[DRY-RUN] Would create device '{names[did]}' ({device_type}), place it ({placement}), "
                        f"allocate IPs (/32 & /31), and create {len(mappings_by_id[did])} cable(s)."
                    )
                return f"Dry-run complete for {len(device_ids)} device(s)."

            created = []
            for idx, did in enumerate(device_ids):
                p_rack, p_pos = placements[did]
                # Units in this device's rack reserved by explicitly-positioned devices not yet created
                reserved = [
                    u
                    for later in device_ids[idx + 1:]
                    for (r_pk, u) in units_by_id.get(later, [])
                    if p_rack and r_pk == p_rack.pk
                ]
                device = self._commission_device(
                    data, names[did], mappings_by_id[did], p_rack, p_pos, reserved
                )
                created.append(device.name)

            return f"Commissioning complete for {len(created)} device(s): {', '.join(created)}."

    def _commission_device(
        self, data, composed_name: str, mappings, rack=None, rack_position=None, reserved=()
    ) -> Device:
        site = data["site"]
        tenant = data["tenant"]
        device_type = data["device_type"]
        role = data["role"]
        platform = data.get("platform")
        status = data["status"]
        rack_face = data["rack_face"]
        enforce_site_match = data["enforce_patch_plan_site_match"]

        # Create device (interfaces from DeviceType templates are instantiated on save)
        device = Device(
            name=composed_name,
            site=site,
            tenant=tenant,
            device_type=device_type,
            role=role,
            platform=platform,
            status=status,
           # asset_tag=composed_name,  # keep device_id as asset_tag as well
        )
        device.full_clean()
        device.save()
        self.log_success(
            f"Created device: {device.name} (site={site}, type={device_type}, role={role})"
        )
        # Create module bays per device type (if a plan exists)
        # self._create_module_bays_for_device(device)

        # Optional rack placement
        if rack:
            device.rack = rack
            device.face = rack_face

            if rack_position is not None:
                device.position = rack_position
                # Validate explicit placement
                try:
                    device.full_clean()
                except ValidationError as e:
                    raise AbortScript(
                        f"Cannot place {device.name} in rack {rack} face={rack_face} U={rack_position}: "
                        f"{'; '.join(e.messages)}"
                    )
                device.save()
                self.log_success(
                    f"Placed device in rack {rack} face={rack_face} U={rack_position}"
                )
                # self.log_success(
                #     f"Placed device in rack {rack}  U={rack_position}"
                #  )
            else:
                # Try to auto-place by probing available positions
                self._auto_place_in_rack(device, rack, rack_face, reserved)

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

        # Optionally pick one host offset that is free in every single-IP prefix
        host_offset = None
        if data.get("align_host_octet", True):
            labels = [
                self._normalize(getattr(i, "label", None))
                for i in interfaces
                if self._normalize(getattr(i, "label", None)) in single_ip_labels
            ]
            duplicates = sorted({l for l in labels if labels.count(l) > 1})
            if duplicates:
                raise AbortScript(
                    f"{device.name}: label(s) {', '.join(duplicates)} used on more than one interface; "
                    "cannot use the same last octet. Untick 'Use the same last octet' or fix the labels."
                )
            if labels:
                parents = {l: self._find_site_prefix_by_tag(site, l) for l in labels}
                host_offset = self._find_common_host_offset(list(parents.values()))
                if host_offset is None:
                    raise AbortScript(
                        f"{device.name}: no host number is free in all of "
                        f"{', '.join(str(p.prefix) for p in parents.values())}. "
                        "Untick 'Use the same last octet' to allocate independently."
                    )
                self.log_info(f"{device.name}: using host number .{host_offset} for {', '.join(sorted(labels))}")

        for iface in interfaces:
            label = self._normalize(getattr(iface, "label", None))
            if not label:
                # Ignore unlabeled interfaces
                continue

            # /32 IP allocation for label-tag-matched prefixes
            if label in single_ip_labels:
                parent = self._find_site_prefix_by_tag(site, label)
                if host_offset is not None:
                    addr = self._ip_at_offset(parent, host_offset)
                else:
                    addr = self._allocate_next_ip(parent)
                if label in self.HOST_ROUTE_LABELS:
                    # Loopbacks get a /32; oob-mgmt keeps the parent prefix length
                    addr = f"{addr.split('/')[0]}/32"

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
                addr2 = self._allocate_next_ip(child)
                # First usable IP from the /31 (network + 1)
                net = ipaddress.ip_network(str(child.prefix))
                hosts = list(net.hosts())
                if len(hosts) < 1:
                    raise AbortScript(f"Allocated /31 {child.prefix} has no usable host IPs.")

                first_ip = str(hosts[0])

                ip_obj = IPAddress(
                    address=addr2,
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

        # Optional cabling via patch plan (mappings already filtered to this device)
        if mappings:
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

        return device
