# File: netbox/scripts/commission_device.py
#
# NetBox Custom Script: Commission a new device and auto-allocate IPs from site-scoped tagged prefixes.
#
# Notes:
# - Interfaces are instantiated automatically from the Device Type at device creation time.
#   (Device Type interface templates -> Device interfaces)
# - Prefix.get_first_available_ip() returns the first IP that does not yet exist in the DB for that prefix.

from extras.scripts import Script
from extras.scripts import StringVar, ObjectVar, ChoiceVar, BooleanVar

from dcim.models import Device, DeviceRole, DeviceType, Platform, Site
from ipam.models import IPAddress, Prefix
from tenancy.models import Tenant
from utilities.exceptions import AbortScript




class CommissionDevice(Script):
    class Meta:
        name = "Commission New Device (Auto IP Allocation)"
        description = (
            "Creates a new device and allocates next available IPs from site-matched prefixes "
            "whose tags match interface labels (e.g. mgmt_subnet, dmz_a_subnet, dmz_b_subnet, rtp_subnet)."
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
        )

    hostname = StringVar(
        label="Device hostname",
        description="Name to assign to the device (must be unique within NetBox)",
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
            "If enabled, any interface whose label matches one of the known subnet tags "
            "will get an IP. If disabled, only mgmt_subnet will be allocated."
        ),
        default=True,
        required=True,
    )

    # The interface labels you want to drive allocation from
    # (these must match Tag names applied to the Prefix objects)
    SUBNET_LABELS = (
        "mgmt_subnet",
        "dmz_a_subnet",
        "dmz_b_subnet",
        "rtp_subnet",
    )

    MGMT_LABEL = "mgmt_subnet"


    def _find_site_prefix_by_tag(self, site, tag_name) -> Prefix:
        """
        Find a Prefix at the given site having a Tag with name == tag_name.
        If multiple exist, pick the most specific (largest prefix length).
        NetBox 3.7.x does not have a 'prefix_length' DB field, so we sort in Python.
        """
        qs = Prefix.objects.filter(site=site, tags__name=tag_name)
    
        if not qs.exists():
            raise AbortScript(
                f"No prefix found for site='{site}' with tag='{tag_name}'. "
                "Ensure Prefix.site is set and the Prefix is tagged correctly."
            )
    
        # Sort by prefix length (most specific first), then by prefix for stability
        candidates = list(qs)
        candidates.sort(key=lambda p: (p.prefix.prefixlen, str(p.prefix)), reverse=True)
    
        return candidates[0]


    # def _find_site_prefix_by_tag(self, site: Site, tag_name: str) -> Prefix:
    #     """
    #     Find a prefix at the given site having a tag with name == tag_name.
    #     If multiple exist, pick the most specific (largest prefix length).
    #     """
    #     qs = (
    #         Prefix.objects.filter(site=site, tags__name=tag_name)
    #         .order_by("-prefix_length")  # more specific first
    #     )
    #     if not qs.exists():
    #         raise AbortScript(
    #             f"No prefix found for site='{site}' with tag='{tag_name}'. "
    #             "Ensure the Prefix has Site set and is tagged correctly."
    #         )
    #     # If you prefer a deterministic choice beyond prefix_length, add additional ordering here
    #     return qs.first()

    def _allocate_next_ip(self, prefix: Prefix) -> str:
        """
        Return the next available IP (string like '10.0.0.12/24') from the prefix.
        Uses Prefix.get_first_available_ip(), which returns the first IP not existing in DB.
        """
        ip = prefix.get_first_available_ip()
        if not ip:
            raise AbortScript(f"No available IPs left in prefix {prefix.prefix}.")
        # ip may be an IPNetwork / IPAddress-like object depending on version; str() is safe for address/prefixlen
        return str(ip)

    def run(self, data, commit):
        hostname = data["hostname"].strip()
        site = data["site"]
        platform = data["platform"]
        device_type = data["device_type"]
        role = data.get("role")
        tenant = data.get("tenant")
        status = data["status"]
        allocate_all = data["allocate_all_labeled_interfaces"]

        # 1) Create the device
        if Device.objects.filter(name=hostname).exists():
            raise AbortScript(f"Device name '{hostname}' already exists in NetBox.")

        device = Device(
            name=hostname,
            site=site,
            platform=platform,
            device_type=device_type,
            role=role,
            tenant=tenant,
            status=status,
        )

        if commit:
            device.save()
            self.log_success(f"Created device: {device.name} (site={site}, type={device_type}, platform={platform})")
        else:
            self.log_info(f"[DRY-RUN] Would create device: {hostname}")
            return

        # At this point, interfaces should be instantiated from device type templates automatically.

        # 2) Select interfaces to allocate
        interfaces = list(device.interfaces.all())

        if not interfaces:
            raise AbortScript(
                "No interfaces found on device after creation. "
                "Check that the Device Type has interface templates defined."
            )

        # Build target set
        target_labels = set(self.SUBNET_LABELS) if allocate_all else {self.MGMT_LABEL}

        # Filter interfaces by label
        target_ifaces = []
        for iface in interfaces:
            # Interface.label is optional; make sure your templates populate it.
            if iface.label and iface.label.strip() in target_labels:
                target_ifaces.append(iface)

        if not target_ifaces:
            raise AbortScript(
                "No interfaces matched the required labels. "
                "Ensure Interface.label on the device matches one of: "
                f"{', '.join(sorted(target_labels))}"
            )

        # 3) Allocate IPs per interface label -> tagged prefix
        mgmt_ip_obj = None

        for iface in target_ifaces:
            label = iface.label.strip()

            prefix = self._find_site_prefix_by_tag(site, label)
            addr = self._allocate_next_ip(prefix)

            # Create IP and assign to interface
            ip_obj = IPAddress(
                address=addr,
                vrf=prefix.vrf,
                tenant=tenant or prefix.tenant,
                status="active",
                assigned_object=iface,
            )

            ip_obj.full_clean()

            ip_obj.save()
            self.log_success(f"Assigned {ip_obj.address} to {device.name} / {iface.name} (label={label}, prefix={prefix.prefix})")

            if label == self.MGMT_LABEL:
                mgmt_ip_obj = ip_obj

        # 4) Set primary IPv4 = mgmt_subnet
        if mgmt_ip_obj:
            device.primary_ip4 = mgmt_ip_obj
            device.save()
            self.log_success(f"Set primary IPv4 for {device.name} to {mgmt_ip_obj.address}")
        else:
            self.log_warning(
                f"No '{self.MGMT_LABEL}' interface was allocated, so primary IPv4 was not set."
            )

        return f"Commissioning complete for {device.name}."
