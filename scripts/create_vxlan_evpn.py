import ipaddress
import json

from dcim.models import Location, Site
from django.utils.text import slugify
from extras.scripts import BooleanVar, IntegerVar, ObjectVar, Script, StringVar
from ipam.models import Prefix, VRF
from vpn.models import L2VPN


class GenerateVxlanFabricAddressing(Script):
    class Meta:
        name = "Create VXLAN-EVPN"
        description = (
            "Generate VXLAN EVPN values for a selected workload prefix and "
            "create an L2VPN instance in NetBox."
        )
        field_order = [
            "site",
            "pod_location",
            "vxlan_name",
            "vxlan_serviceid",
            "vrf_name",
            "workload_prefix",
            "reuse_l3_vni",
            "existing_l3_vni",
        ]

    site = ObjectVar(
        model=Site,
        label="Site",
        required=True,
    )

    pod_location = ObjectVar(
        model=Location,
        label="Pod Location",
        required=True,
        description="Location whose pod_id custom field stores the L2 VNI base (e.g. 1010000).",
        query_params={"site_id": "$site"},
    )

    vxlan_name = StringVar(
        label="VXLAN Name",
        required=True,
    )

    vxlan_serviceid = IntegerVar(
        label="VXLAN Service ID",
        required=True,
        description="Numeric service identifier from 1000 through 1999.",
    )

    vrf_name = ObjectVar(
        model=VRF,
        label="VRF Name",
        required=False,
        description="VRF to associate with this VXLAN (e.g. VRF-1)",
    )

    workload_prefix = ObjectVar(
        model=Prefix,
        label="Workload Prefix",
        description="Select the workload subnet from IPAM",
        required=True,
        query_params={"scope_type": "dcim.site", "scope_id": "$site"},
    )

    reuse_l3_vni = BooleanVar(
        label="Reuse existing L3 VNI/RF",
        required=False,
        default=False,
        description="Reuse L3 VNI, L3 VLAN, and firewall transit VLAN from an existing VXLAN-EVPN record.",
    )

    existing_l3_vni = ObjectVar(
        model=L2VPN,
        label="Existing L3 VNI/RF source",
        required=False,
        description="Required when reusing L3 VNI/RF values.",
        query_params={"type": "vxlan-evpn"},
    )

    def calculate_values(self, prefix, service_id, l2_vni_base):
        network = ipaddress.ip_network(str(prefix.prefix), strict=False)
        octets = str(network.network_address).split(".")
        subnet_id_1 = int(octets[2])
        subnet_id_2 = int(octets[3])
        multicast_last_octet = subnet_id_2 + 1
        l3_vni_base = l2_vni_base + 4000000

        if multicast_last_octet > 255:
            raise ValueError(
                f"Cannot derive multicast group for {network}: fourth octet plus one exceeds 255."
            )

        return {
            "network": network,
            "prefix_len": network.prefixlen,
            "multicast_group": f"239.0.{subnet_id_1}.{multicast_last_octet}",
            "workload_vlan": service_id,
            "workload_vni": l2_vni_base + service_id,
            "workload_gateway": str(network.network_address + 1),
            "new_l3_vni_vlan": 1000 + service_id,
            "new_l3_vni": l3_vni_base + service_id,
            "new_fw_transit_vlan": 2000 + service_id,
            "l2_vni_base": l2_vni_base,
            "l3_vni_base": l3_vni_base,
        }

    def get_reused_l3_values(self, existing_l3_vni, l2_vni_base):
        if not existing_l3_vni:
            raise ValueError(
                "Select an existing L3 VNI/RF source when 'Reuse existing L3 VNI/RF' is enabled."
            )

        custom_fields = existing_l3_vni.custom_field_data or {}
        missing_fields = [
            field
            for field in ("L3VNI", "l3_vlan", "fw_transit_vlan")
            if custom_fields.get(field) in (None, "")
        ]

        if missing_fields:
            raise ValueError(
                f"Existing L3 VNI/RF source is missing custom field(s): {', '.join(missing_fields)}"
            )

        source_pod_id = custom_fields.get("pod_id")
        if source_pod_id is not None and int(source_pod_id) != l2_vni_base:
            raise ValueError(
                f"Existing L3 VNI/RF source uses pod base {source_pod_id}; "
                f"select a source with pod base {l2_vni_base}."
            )

        return {
            "l3_vni": custom_fields["L3VNI"],
            "l3_vni_vlan": custom_fields["l3_vlan"],
            "fw_transit_vlan": custom_fields["fw_transit_vlan"],
        }

    def validate_unique_l2_values(self, service_id, l2_vni, l2_vni_base, site):
        l2_vni_conflict = L2VPN.objects.filter(identifier=l2_vni).first()
        if l2_vni_conflict:
            raise ValueError(
                f"L2 VNI {l2_vni} is already used by L2VPN '{l2_vni_conflict}'. "
                "Choose a unique VXLAN Service ID for this pod."
            )

        workload_vni_conflict = L2VPN.objects.filter(
            custom_field_data__workload_VNI=l2_vni
        ).first()
        if workload_vni_conflict:
            raise ValueError(
                f"L2 VNI {l2_vni} is already recorded on L2VPN '{workload_vni_conflict}'. "
                "Choose a unique VXLAN Service ID for this pod."
            )

        service_id_conflict = L2VPN.objects.filter(
            name__startswith=f"{site}-",
            custom_field_data__pod_id=l2_vni_base,
            custom_field_data__vxlan_serviceid=service_id,
        ).first()
        if service_id_conflict:
            raise ValueError(
                f"VXLAN Service ID {service_id} is already used in pod base {l2_vni_base} "
                f"by L2VPN '{service_id_conflict}'. Choose a unique service ID for this pod."
            )

    def validate_service_id_range(self, service_id):
        if service_id < 1000 or service_id > 1999:
            raise ValueError("VXLAN Service ID must be between 1000 and 1999.")

    def validate_prefix_site_scope(self, prefix, site):
        prefix_scope = getattr(prefix, "scope", None)

        if prefix_scope == site:
            return

        legacy_site = getattr(prefix, "site", None)
        if legacy_site == site:
            return

        if prefix_scope is None and legacy_site is None:
            raise ValueError(
                f"Workload prefix {prefix} must be scoped to site '{site}'."
            )

        raise ValueError(
            f"Workload prefix {prefix} is scoped to '{prefix_scope or legacy_site}', "
            f"but selected site is '{site}'."
        )

    def get_pod_vni_base(self, pod_location):
        custom_fields = pod_location.custom_field_data or {}
        pod_vni_base = custom_fields.get("pod_id")

        if pod_vni_base in (None, ""):
            raise ValueError(
                f"Pod location '{pod_location}' is missing the pod_id custom field."
            )

        return int(pod_vni_base)

    def validate_pod_vni_base(self, pod_vni_base):
        if pod_vni_base < 1010000 or pod_vni_base > 1990000:
            raise ValueError(
                "Pod location pod_id must be an L2 VNI base from 1010000 through 1990000."
            )
        if pod_vni_base % 10000 != 0:
            raise ValueError(
                "Pod location pod_id must be aligned to a 10000 boundary, e.g. 1010000."
            )

    def add_new_l3_values(self, values):
        values.update(
            {
                "l3_vni": values["new_l3_vni"],
                "l3_vni_vlan": values["new_l3_vni_vlan"],
                "fw_transit_vlan": values["new_fw_transit_vlan"],
            }
        )

    def log_l3_values(self, values):
        self.log_info(f"L3 VNI VLAN       : {values['l3_vni_vlan']}")
        self.log_info(f"L3 VNI            : {values['l3_vni']}")
        self.log_info(f"FW Transit VLAN   : {values['fw_transit_vlan']}")

    def create_l2vpn(
        self,
        *,
        name,
        identifier,
        status,
        vxlan_type,
        comments,
        custom_fields,
        commit=True,
    ):
        l2vpn = L2VPN(
            name=name,
            slug=slugify(name),
            identifier=identifier,
            status=status,
            type=vxlan_type,
            comments=comments,
        )

        l2vpn.custom_field_data = l2vpn.custom_field_data or {}
        for cf_name, cf_value in custom_fields.items():
            l2vpn.custom_field_data[cf_name] = cf_value

        if commit:
            l2vpn.full_clean()
            l2vpn.save()

        self.log_success(f"Created L2VPN: {name}")
        return l2vpn

    def run(self, data, commit):
        prefix = data["workload_prefix"]
        pod_location = data["pod_location"]
        vxlan_name = data["vxlan_name"]
        vxlan_serviceid = data["vxlan_serviceid"]
        vrf_name = data["vrf_name"]
        site = data["site"]
        reuse_l3_vni = data.get("reuse_l3_vni")
        has_vrf = vrf_name is not None
        vxlan_scope = "L3VXLAN" if has_vrf else "L2VXLAN"

        l2_vni_base = self.get_pod_vni_base(pod_location)
        self.validate_pod_vni_base(l2_vni_base)
        self.validate_service_id_range(vxlan_serviceid)
        self.validate_prefix_site_scope(prefix, site)
        values = self.calculate_values(prefix, vxlan_serviceid, l2_vni_base)
        self.validate_unique_l2_values(
            vxlan_serviceid, values["workload_vni"], l2_vni_base, site
        )

        if reuse_l3_vni and not has_vrf:
            raise ValueError(
                "Select a VRF before reusing an existing L3 VNI/RF source."
            )

        if has_vrf and reuse_l3_vni:
            reused_values = self.get_reused_l3_values(
                data.get("existing_l3_vni"), l2_vni_base
            )
            values.update(reused_values)
            self.log_info(
                f"Reusing L3 VNI {values['l3_vni']}, L3 VLAN {values['l3_vni_vlan']}, "
                f"and FW Transit VLAN {values['fw_transit_vlan']}"
            )
        elif has_vrf:
            self.add_new_l3_values(values)

        network = values["network"]

        self.log_success("VXLAN Fabric Addressing Generated")
        self.log_info(f"VXLAN Scope       : {vxlan_scope}")
        self.log_info(f"Pod Location      : {pod_location}")
        self.log_info(f"L2 VNI Base       : {values['l2_vni_base']}")
        if has_vrf:
            self.log_info(f"L3 VNI Base       : {values['l3_vni_base']}")
        self.log_info(f"SERVICE_ID        : {vxlan_serviceid}")
        self.log_info(f"Subnet            : {network}")
        self.log_info(f"VRF Name          : {vrf_name.name if vrf_name else 'None'}")
        self.log_info(f"VRF Length        : {values['prefix_len']}")
        self.log_info(f"Multicast Group   : {values['multicast_group']}")
        if has_vrf:
            self.log_l3_values(values)
        self.log_info(f"Workload VLAN     : {values['workload_vlan']}")
        self.log_info(f"Workload VNI      : {values['workload_vni']}")
        self.log_info(f"Workload Gateway  : {values['workload_gateway']}")

        output_data = {
            "VXLAN Scope": vxlan_scope,
            "Pod Location": pod_location.name,
            "L2 VNI Base": values["l2_vni_base"],
            "VXLAN Service ID": vxlan_serviceid,
            "Subnet": str(network),
            "VRF Name": vrf_name.name if vrf_name else None,
            "Multicast Group": values["multicast_group"],
            "Workload VLAN": values["workload_vlan"],
            "Workload VNI": values["workload_vni"],
            "Workload Gateway": values["workload_gateway"],
            "Reused L3 VNI/RF": bool(has_vrf and reuse_l3_vni),
        }

        custom_fields = {
            "pod_id": values["l2_vni_base"],
            "vxlan_serviceid": vxlan_serviceid,
            "vrf_name": vrf_name.name if vrf_name else None,
            "vxlan_mcast_group": values["multicast_group"],
            "workload_VLAN_ID": values["workload_vlan"],
            "workload_VNI": values["workload_vni"],
            "workload_subnet": prefix.pk,
            "workload_gateway": values["workload_gateway"],
        }

        if has_vrf:
            output_data.update(
                {
                    "L3 VNI VLAN": values["l3_vni_vlan"],
                    "L3 VNI": values["l3_vni"],
                    "FW Transit VLAN": values["fw_transit_vlan"],
                }
            )
            custom_fields.update(
                {
                    "fw_transit_vlan": values["fw_transit_vlan"],
                    "l3_vlan": values["l3_vni_vlan"],
                    "L3VNI": values["l3_vni"],
                }
            )

        self.create_l2vpn(
            name=f"{site}-{pod_location.name}-{vxlan_scope}-{vxlan_name}",
            identifier=values["workload_vni"],
            status="active",
            vxlan_type="vxlan-evpn",
            comments=json.dumps(output_data, indent=2),
            custom_fields=custom_fields,
            commit=commit,
        )
