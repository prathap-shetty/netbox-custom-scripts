from extras.scripts import Script, ObjectVar,StringVar, IntVar
from django.utils.text import slugify
from ipam.models import Prefix, Vrf
from vpn.models import L2VPN
import ipaddress
from dcim.models import (    
    Site)


class GenerateVxlanFabricAddressing(Script):

    class Meta:
        name = "Create VXLAN-EVPN"
        description = "Generate VXLAN EVPN values for a selected workload prefix and create an L2VPN instance in NetBox with the calculated values."
        field_order = ["vxlan_name", "workload_prefix"]
    
    site = ObjectVar(
        model=Site,
        label="Site ",
        required=True 
    )
    
    vxlan_name = StringVar(
        label="VXLAN Name",
        required=True,
    )
    vxlan_serviceid = IntVar(
        label="VXLAN Service ID",
        required=True,
        description="Numeric Service identifier (e.g., 1001)",
        range=(1000, 9999)
    )

    vrf_name = ObjectVar(
        model=Vrf,
        label="VRF Name",
        required=True,
        description="Name of the VRF to associate with this VXLAN (e.g., VRF-1)"
    )

    # ✅ IPAM prefix selector
    workload_prefix = ObjectVar(
        model=Prefix,
        description="Select the workload subnet from IPAM",
        required=True,
    )

    def allocate_vni(self, vxlan_serviceid):
        """
        Allocate a VNI based on VRF ID, Subnet ID, and Segment ID
        Format: VRF(4 digits) + Subnet(3 digits) + Segment(2 digits)
        Example: VRF 1001, Subnet 10, Segment 1 -> VNI 1001001001
        """
        return {
            "l2_vni": 1000000 + vxlan_serviceid,  # Example: Service ID 1001 -> L2 VNI 1001001
            "l3_vni": 5000000 + vxlan_serviceid,  # Example: Service ID 1001 -> L3 VNI 5001001
        }

    def update_l2vpn(
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
        """
        Create or update an L2VPN based on identifier
        """

        l2vpn, created = L2VPN.objects.get_or_create(
            identifier=identifier,
            defaults={
                "name": name,
                "slug": slugify(name),
                "type": vxlan_type,
                "status": status,
            },
        )

        if created:
            self.log_success(f"Created L2VPN: {name}")
        else:
            self.log_info(f"Updating existing L2VPN: {name}")

        # ------------------------------
        # Standard fields
        # ------------------------------
        l2vpn.name = name
        l2vpn.slug = slugify(name)
        l2vpn.status = status
        l2vpn.type = vxlan_type
        l2vpn.comments = comments

        # ------------------------------
        # Custom fields
        # ------------------------------
        for cf_name, cf_value in custom_fields.items():

            # Prefix-based custom field
            if cf_name == "workload_subnet" and isinstance(cf_value, Prefix):
                l2vpn.custom_field_data[cf_name] = cf_value
                continue

            l2vpn.custom_field_data[cf_name] = cf_value

        if commit:
            l2vpn.save()

        return l2vpn

    def run(self, data, commit):

        prefix = data["workload_prefix"]
        vxlan_name = data["vxlan_name"]
        vxlan_serviceid = data["vxlan_serviceid"]
        vrf_name = data["vrf_name"]
        site = data["site"]
        network = ipaddress.ip_network(prefix.prefix)

        # --- Extract address components ---
        octets = str(network.network_address).split(".")
        SUBNET_ID_1 = int(octets[2])
        SUBNET_ID_2 = int(octets[3])

        VRF_ID = vxlan_serviceid

        prefix_len = network.prefixlen

        # --- Fabric values ---
        multicast_group = f"239.0.{SUBNET_ID_1}.{SUBNET_ID_2}"

        l3_vni_vlan = 2000 + SUBNET_ID_1 + SUBNET_ID_2  # Example: Subnet ID 10 -> VLAN 110
        l3_vni = 500000 + vxlan_serviceid  # Example: Service ID 1001 -> L3 VNI VLAN 5001001

        workload_vlan = 1000 + SUBNET_ID_1 + SUBNET_ID_2
        workload_vni = 100000 + vxlan_serviceid  # Example: Service ID 1001 -> Workload VNI 1001001

        fw_transit_vlan = 100 + SUBNET_ID_1 + SUBNET_ID_2

        # --- Output ---
        self.log_success("VXLAN Fabric Addressing Generated")
        self.log_info(f"SERVICE_ID = {vxlan_serviceid}}, L3_SEGMENT_ID = {l3_vni}, WORKLOAD_SEGMENT_ID = {workload_vlan}")
        self.log_info(f"Subnet            : {network}")
        self.log_info(f"VRF Name          : {vrf_name}")
        self.log_info(f"VRF Length        : {prefix_len}")
        self.log_info(f"Multicast Group   : {multicast_group}")
        self.log_info(f"L3 VNI VLAN       : {l3_vni_vlan}")
        self.log_info(f"L3 VNI            : {l3_vni}")
        self.log_info(f"Workload VLAN     : {workload_vlan}")
        self.log_info(f"Workload VNI      : {workload_vni}")
        self.log_info(f"FW Transit VLAN   : {fw_transit_vlan}")
        self.log_info(f"Workload Gateway  : {network.network_address + 1}")  # Assuming gateway is the first IP in the subnet
        
        output_data =  {
            "Subnet": str(network),
            "Multicast Group": multicast_group,
            "L3 VNI VLAN": l3_vni_vlan,
            "L3 VNI": l3_vni,
            "Workload VLAN": workload_vlan,
            "Workload VNI": workload_vni,
            "FW Transit VLAN": fw_transit_vlan,
            "workload_gateway": str(network.network_address + 1)  # Assuming gateway is the first IP in the subnet
        }

        
        self.update_l2vpn(
            name=f"{site}-{vxlan_name}",
            identifier=workload_vni,
            status="active",
            vxlan_type="vxlan-evpn",
            comments=output_data,
            custom_fields={
                "fw_transit_vlan": fw_transit_vlan,
                "l3_vlan": l3_vni_vlan,
                "L3VNI": l3_vni,
                "vxlan_mcast_group": multicast_group,
                "workload_VLAN_ID": workload_vlan,
                "workload_VNI": workload_vni,
                "workload_subnet": prefix.pk,
                "workload_gateway": str(network.network_address + 1)  # Assuming gateway is the first IP in the subnet
            },
            commit=commit,
        )
