from extras.scripts import Script
from ipam.models import Prefix
from django.utils.text import slugify
import ipaddress


class GenerateVxlanFabricAddressing(Script):

    class Meta:
        name = "Generate VXLAN Fabric Addressing"
        description = "Generate VXLAN/VLAN values from a workload subnet"
        field_order = ["workload_prefix"]

    workload_prefix = Prefix.objects.all()

    def run(self, data, commit):

        prefix = data["workload_prefix"]
        network = ipaddress.ip_network(prefix.prefix)

        # --- Extract subnet components ---
        octets = str(network.network_address).split(".")
        SUBNET_ID = int(octets[2])       # 3rd octet
        VRF_ID = SUBNET_ID

        prefix_length = network.prefixlen

        # --- Segment logic ---
        if prefix_length == 24:
            SEGMENT_ID = 0
            NETWORK_ID = 0
        else:
            block_size = network.num_addresses
            full_24 = ipaddress.ip_network(f"{octets[0]}.{octets[1]}.{octets[2]}.0/24")
            NETWORK_ID = int(network.network_address) - int(full_24.network_address)
            SEGMENT_ID = NETWORK_ID // block_size

        # --- Addressing outputs ---
        multicast_group = f"239.0.0.{VRF_ID}"

        l3_vni_vlan = VRF_ID
        l3_vni = f"{VRF_ID:04d}0000"

        workload_vlan = (SUBNET_ID * 10) + SEGMENT_ID
        workload_vni = f"{VRF_ID:04d}{SUBNET_ID:03d}{SEGMENT_ID}"

        fw_transit_vlan = (VRF_ID * 10) + 9

        # --- Output ---
        self.log_success("VXLAN Fabric Addressing Generated")
        self.log_info(f"IP Subnet         : {network}")
        self.log_info(f"Multicast Group  : {multicast_group}")
        self.log_info(f"L3 VNI VLAN      : {l3_vni_vlan}")
        self.log_info(f"L3 VNI           : {l3_vni}")
        self.log_info(f"Workload VLAN    : {workload_vlan}")
        self.log_info(f"Workload VNI     : {workload_vni}")
        self.log_info(f"FW Transit VLAN  : {fw_transit_vlan}")

        return {
            "Subnet": str(network),
            "Multicast Group": multicast_group,
            "L3 VNI VLAN": l3_vni_vlan,
            "L3 VNI": l3_vni,
            "Workload VLAN": workload_vlan,
            "Workload VNI": workload_vni,
            "FW Transit VLAN": fw_transit_vlan,
        }
