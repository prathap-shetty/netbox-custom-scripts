# from extras.scripts import Script, ObjectVar
# from ipam.models import Prefix
# import ipaddress


# class GenerateVxlanFabricAddressing(Script):

#     class Meta:
#         name = "Generate VXLAN Fabric Addressing"
#         description = "Generate VXLAN Fabric values from a selected IPAM workload subnet"
#         field_order = ["workload_prefix"]

#     # ✅ IPAM prefix selector
#     workload_prefix = ObjectVar(
#         model=Prefix,
#         description="Select the workload subnet from IPAM"
#     )

#     def run(self, data, commit):

#         prefix = data["workload_prefix"]
#         network = ipaddress.ip_network(prefix.prefix)

#         # --- Extract address components ---
#         octets = str(network.network_address).split(".")
#         SUBNET_ID = int(octets[2])
#         VRF_ID = SUBNET_ID

#         prefix_len = network.prefixlen

#         # --- Segment calculation ---
#         if prefix_len == 24:
#             SEGMENT_ID = 0
#             NETWORK_ID = 0
#         else:
#             full_24 = ipaddress.ip_network(
#                 f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
#             )
#             block_size = network.num_addresses
#             NETWORK_ID = int(network.network_address) - int(full_24.network_address)
#             SEGMENT_ID = NETWORK_ID // block_size

#         # --- Fabric values ---
#         multicast_group = f"239.0.{SEGMENT_ID}.{VRF_ID}"

#         l3_vni_vlan = VRF_ID
#         l3_vni = f"{VRF_ID:04d}0000"

#         workload_vlan = f"{SUBNET_ID}" + f"{SEGMENT_ID}"
#         workload_vni = f"{VRF_ID:04d}{SUBNET_ID:03d}{SEGMENT_ID}"

#         fw_transit_vlan = f"{VRF_ID}" + f"9"

#         # --- Output ---
#         self.log_success("VXLAN Fabric Addressing Generated")
#         self.log_info(f"NETWORK_ID = {NETWORK_ID}, SEGMENT_ID = {SEGMENT_ID}")
#         self.log_info(f"Subnet            : {network}")
#         self.log_info(f"Multicast Group   : {multicast_group}")
#         self.log_info(f"L3 VNI VLAN       : {l3_vni_vlan}")
#         self.log_info(f"L3 VNI            : {l3_vni}")
#         self.log_info(f"Workload VLAN     : {workload_vlan}")
#         self.log_info(f"Workload VNI      : {workload_vni}")
#         self.log_info(f"FW Transit VLAN   : {fw_transit_vlan}")

#         return {
#             "Subnet": str(network),
#             "Multicast Group": multicast_group,
#             "L3 VNI VLAN": l3_vni_vlan,
#             "L3 VNI": l3_vni,
#             "Workload VLAN": workload_vlan,
#             "Workload VNI": workload_vni,
#             "FW Transit VLAN": fw_transit_vlan,
#         }

from extras.scripts import Script, ObjectVar,StringVar
from django.utils.text import slugify
from ipam.models import Prefix
from vpn.models import L2VPN
import ipaddress
from dcim.models import (    
    Site)


class GenerateVxlanFabricAddressing(Script):

    class Meta:
        name = "Generate VXLAN Fabric Addressing"
        description = "Generate VXLAN Fabric values from a selected IPAM workload subnet"
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
    
    # ✅ IPAM prefix selector
    workload_prefix = ObjectVar(
        model=Prefix,
        description="Select the workload subnet from IPAM",
        required=True,
    )

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
        site = data["site"]
        network = ipaddress.ip_network(prefix.prefix)

        # --- Extract address components ---
        octets = str(network.network_address).split(".")
        SUBNET_ID = int(octets[2])
        VRF_ID = SUBNET_ID

        prefix_len = network.prefixlen

        # --- Segment calculation ---
        if prefix_len == 24:
            SEGMENT_ID = 0
            NETWORK_ID = 0
        else:
            full_24 = ipaddress.ip_network(
                f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
            )
            block_size = network.num_addresses
            NETWORK_ID = int(network.network_address) - int(full_24.network_address)
            SEGMENT_ID = NETWORK_ID // block_size

        # --- Fabric values ---
        multicast_group = f"239.0.{SEGMENT_ID}.{VRF_ID}"

        l3_vni_vlan = VRF_ID
        l3_vni = f"{VRF_ID:04d}0000"

        workload_vlan = f"{SUBNET_ID}" + f"{SEGMENT_ID}"
        workload_vni = f"{VRF_ID:04d}{SUBNET_ID:03d}{SEGMENT_ID}"

        fw_transit_vlan = f"{VRF_ID}" + f"9"

        # --- Output ---
        self.log_success("VXLAN Fabric Addressing Generated")
        self.log_info(f"NETWORK_ID = {NETWORK_ID}, SEGMENT_ID = {SEGMENT_ID}")
        self.log_info(f"Subnet            : {network}")
        self.log_info(f"Multicast Group   : {multicast_group}")
        self.log_info(f"L3 VNI VLAN       : {l3_vni_vlan}")
        self.log_info(f"L3 VNI            : {l3_vni}")
        self.log_info(f"Workload VLAN     : {workload_vlan}")
        self.log_info(f"Workload VNI      : {workload_vni}")
        self.log_info(f"FW Transit VLAN   : {fw_transit_vlan}")
        
        output_data =  {
            "Subnet": str(network),
            "Multicast Group": multicast_group,
            "L3 VNI VLAN": l3_vni_vlan,
            "L3 VNI": l3_vni,
            "Workload VLAN": workload_vlan,
            "Workload VNI": workload_vni,
            "FW Transit VLAN": fw_transit_vlan,
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
            },
            commit=commit,
        )


