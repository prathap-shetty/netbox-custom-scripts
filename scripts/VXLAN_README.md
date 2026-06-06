# NetBox VXLAN-EVPN Custom Script

This workspace contains a NetBox custom script for creating VXLAN-EVPN `L2VPN`
records with deterministic VLAN, VNI, multicast, and gateway values.

## Script

The script is located at:

```text
scripts/vxlan_evpn.py
```

The Docker Compose configuration mounts this directory into NetBox as:

```text
/opt/netbox/netbox/scripts
```

## Required NetBox Data

Create one NetBox `Location` per VXLAN pod. The selected location provides the
pod VNI base through a custom field.

Required `Location` custom field:

| Field | Type | Example | Purpose |
| --- | --- | --- | --- |
| `pod_id` | Integer | `1010000` | L2 VNI base for the pod |

Required `L2VPN` custom fields:

| Field | Type | Example |
| --- | --- | --- |
| `pod_id` | Integer | `1010000` |
| `vxlan_serviceid` | Integer | `1000` |
| `vrf_name` | Text | `VRF-1` |
| `vxlan_mcast_group` | Text | `239.0.20.1` |
| `workload_VLAN_ID` | Integer | `1000` |
| `workload_VNI` | Integer | `1011000` |
| `workload_subnet` | Object/Prefix | selected prefix |
| `workload_gateway` | Text/IP | `10.1.20.1` |
| `L3VNI` | Integer | `5011000` |
| `l3_vlan` | Integer | `2000` |
| `fw_transit_vlan` | Integer | `3000` |

The L3-related fields are written only when a VRF is selected.

## Inputs

| Input | Required | Notes |
| --- | --- | --- |
| Site | Yes | Filters the pod location list |
| Pod Location | Yes | Must have `pod_id` custom field set to the L2 VNI base |
| VXLAN Name | Yes | Used in the created L2VPN name |
| VXLAN Service ID | Yes | Must be between `1000` and `1999` |
| VRF Name | No | If omitted, the script creates an L2-only VXLAN record |
| Workload Prefix | Yes | Used for multicast and gateway calculations |
| Reuse existing L3 VNI/RF | No | Only valid when a VRF is selected |
| Existing L3 VNI/RF source | Conditional | Required when reusing L3 values |

## Allocation Rules

Service IDs are unique per site and pod. VLAN IDs may be reused across pods.

For a service ID `S` and pod L2 VNI base `B`:

| Value | Formula |
| --- | --- |
| Workload/L2 VLAN | `S` |
| Workload/L2 VNI | `B + S` |
| L3 VNI base | `B + 4000000` |
| L3 VNI | `(B + 4000000) + S` |
| L3 VLAN | `1000 + S` |
| FW transit VLAN | `2000 + S` |

Example for pod location `vxlan_pod1` with `pod_id = 1010000` and service ID
`1000`:

| Value | Result |
| --- | --- |
| Workload/L2 VLAN | `1000` |
| Workload/L2 VNI | `1011000` |
| L3 VNI base | `5010000` |
| L3 VNI | `5011000` |
| L3 VLAN | `2000` |
| FW transit VLAN | `3000` |

## Multicast And Gateway

The multicast group is derived from the workload prefix network address:

```text
239.0.<third_octet>.<fourth_octet + 1>
```

Example:

```text
10.1.20.0/24 -> 239.0.20.1
```

The workload gateway is the first usable IP in the prefix:

```text
network address + 1
```

## L2-Only Versus L3 VXLAN

If no VRF is selected, the script creates an L2-only VXLAN record and does not
write:

```text
L3VNI
l3_vlan
fw_transit_vlan
```

If a VRF is selected, the script creates an L3 VXLAN record. It can either:

- Generate new L3 VNI, L3 VLAN, and FW transit VLAN values.
- Reuse L3 VNI, L3 VLAN, and FW transit VLAN values from an existing L2VPN.

When reusing L3 values, the source record must belong to the same pod base.

## Created L2VPN Name

Created records use this name format:

```text
{site}-{pod_location.name}-{L2VXLAN|L3VXLAN}-{vxlan_name}
```

Example:

```text
dc2-vxlan_pod1-L3VXLAN-app-prod
```
