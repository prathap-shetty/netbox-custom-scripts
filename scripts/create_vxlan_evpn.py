import ipaddress
import json

from dcim.models import Site
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

    vxlan_name = StringVar(
        label="VXLAN Name",
        required=True,
    )

    vxlan_serviceid = IntegerVar(
        label="VXLAN Service ID",
        required=True,
        description="Numeric service identifier (e.g. 1001)",
    )

    vrf_name = ObjectVar(
        model=VRF,
        label="VRF Name",
        required=True,
        description="VRF to associate with this VXLAN (e.g. VRF-1)",
    )

    workload_prefix = ObjectVar(
        model=Prefix,
        label="Workload Prefix",
        description="Select the workload subnet from IPAM",
        required=True,
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

    def calculate_values(self, prefix, service_id):
        network = ipaddress.ip_network(str(prefix.prefix), strict=False)
        octets = str(network.network_address).split(".")
        subnet_id_1 = int(octets[2])
        subnet_id_2 = int(octets[3])

        return {
            "network": network,
            "prefix_len": network.prefixlen,
            "multicast_group": f"239.0.{subnet_id_1}.{subnet_id_2}",
            "l3_vni_vlan": 2000 + subnet_id_1 + subnet_id_2,
            "l3_vni": 500000 + service_id,
            "workload_vlan": 1000 + subnet_id_1 + subnet_id_2,
            "workload_vni": 100000 + service_id,
            "fw_transit_vlan": 100 + subnet_id_1 + subnet_id_2,
            "workload_gateway": str(network.network_address + 1),
        }

    def get_reused_l3_values(self, existing_l3_vni):
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

        return {
            "l3_vni": custom_fields["L3VNI"],
            "l3_vni_vlan": custom_fields["l3_vlan"],
            "fw_transit_vlan": custom_fields["fw_transit_vlan"],
        }

    def validate_unique_l2_values(self, service_id, l2_vni):
        l2_vni_conflict = L2VPN.objects.filter(identifier=l2_vni).first()
        if l2_vni_conflict:
            raise ValueError(
                f"L2 VNI {l2_vni} is already used by L2VPN '{l2_vni_conflict}'. "
                "Choose a unique VXLAN Service ID."
            )

        workload_vni_conflict = L2VPN.objects.filter(
            custom_field_data__workload_VNI=l2_vni
        ).first()
        if workload_vni_conflict:
            raise ValueError(
                f"L2 VNI {l2_vni} is already recorded on L2VPN '{workload_vni_conflict}'. "
                "Choose a unique VXLAN Service ID."
            )

        service_id_conflict = L2VPN.objects.filter(
            custom_field_data__vxlan_serviceid=service_id
        ).first()
        if service_id_conflict:
            raise ValueError(
                f"VXLAN Service ID {service_id} is already recorded on L2VPN "
                f"'{service_id_conflict}'. Choose a unique service ID."
            )

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
        vxlan_name = data["vxlan_name"]
        vxlan_serviceid = data["vxlan_serviceid"]
        vrf_name = data["vrf_name"]
        site = data["site"]
        reuse_l3_vni = data.get("reuse_l3_vni")

        values = self.calculate_values(prefix, vxlan_serviceid)
        self.validate_unique_l2_values(vxlan_serviceid, values["workload_vni"])

        if reuse_l3_vni:
            reused_values = self.get_reused_l3_values(data.get("existing_l3_vni"))
            values.update(reused_values)
            self.log_info(
                f"Reusing L3 VNI {values['l3_vni']}, L3 VLAN {values['l3_vni_vlan']}, "
                f"and FW Transit VLAN {values['fw_transit_vlan']}"
            )

        network = values["network"]

        self.log_success("VXLAN Fabric Addressing Generated")
        self.log_info(
            "SERVICE_ID = "
            f"{vxlan_serviceid}, L3_SEGMENT_ID = {values['l3_vni']}, "
            f"WORKLOAD_SEGMENT_ID = {values['workload_vlan']}"
        )
        self.log_info(f"Subnet            : {network}")
        self.log_info(f"VRF Name          : {vrf_name}")
        self.log_info(f"VRF Length        : {values['prefix_len']}")
        self.log_info(f"Multicast Group   : {values['multicast_group']}")
        self.log_info(f"L3 VNI VLAN       : {values['l3_vni_vlan']}")
        self.log_info(f"L3 VNI            : {values['l3_vni']}")
        self.log_info(f"Workload VLAN     : {values['workload_vlan']}")
        self.log_info(f"Workload VNI      : {values['workload_vni']}")
        self.log_info(f"FW Transit VLAN   : {values['fw_transit_vlan']}")
        self.log_info(f"Workload Gateway  : {values['workload_gateway']}")

        output_data = {
            "VXLAN Service ID": vxlan_serviceid,
            "Subnet": str(network),
            "Multicast Group": values["multicast_group"],
            "L3 VNI VLAN": values["l3_vni_vlan"],
            "L3 VNI": values["l3_vni"],
            "Workload VLAN": values["workload_vlan"],
            "Workload VNI": values["workload_vni"],
            "FW Transit VLAN": values["fw_transit_vlan"],
            "Workload Gateway": values["workload_gateway"],
            "Reused L3 VNI/RF": bool(reuse_l3_vni),
        }

        self.create_l2vpn(
            name=f"{site}-{vxlan_name}",
            identifier=values["workload_vni"],
            status="active",
            vxlan_type="vxlan-evpn",
            comments=json.dumps(output_data, indent=2),
            custom_fields={
                "fw_transit_vlan": values["fw_transit_vlan"],
                "l3_vlan": values["l3_vni_vlan"],
                "L3VNI": values["l3_vni"],
                "vxlan_mcast_group": values["multicast_group"],
                "workload_VLAN_ID": values["workload_vlan"],
                "workload_VNI": values["workload_vni"],
                "workload_subnet": prefix.pk,
                "workload_gateway": values["workload_gateway"],
            },
            commit=commit,
        )
