from dcim.models import Site
from ipam.models import Prefix
from extras.scripts import Script, ObjectVar, IntegerVar
import ipaddress


class BulkCreateSubnets(Script):
    class Meta:
        name = "Bulk create subnets"
        description = "Create multiple child prefixes from a selected parent prefix"
        field_order = [
            "parent_prefix",
            "child_prefix_length",
            "subnet_count",
        ]

    parent_prefix = ObjectVar(
        model=Prefix,
        description="Select the parent prefix (e.g. 100.86.120.0/23)",
    )

    child_prefix_length = IntegerVar(
        description="Child prefix length (e.g. 26 for /26)",
        min_value=1,
        max_value=32,
    )

    subnet_count = IntegerVar(
        description="Number of child subnets to create",
        min_value=1,
    )

    def run(self, data, commit=True):

        parent = data["parent_prefix"]
        child_length = data["child_prefix_length"]
        count = data["subnet_count"]

        parent_net = ipaddress.ip_network(parent.prefix)

        if child_length <= parent_net.prefixlen:
            self.log_failure(
                f"Child prefix length must be larger than parent prefix length "
                f"(/ {parent_net.prefixlen})"
            )
            return

        available_subnets = list(parent_net.subnets(new_prefix=child_length))

        if count > len(available_subnets):
            self.log_failure(
                f"Requested {count} subnets, but only "
                f"{len(available_subnets)} fit inside {parent.prefix}"
            )
            return

        created = 0

        for subnet in available_subnets:
            if created >= count:
                break

            subnet_str = str(subnet)

            if Prefix.objects.filter(prefix=subnet_str).exists():
                self.log_warning(f"Skipping existing prefix {subnet_str}")
                continue

            Prefix.objects.create(
                prefix=subnet_str,
                parent=parent,
                site=parent.site,
                vrf=parent.vrf,
                tenant=parent.tenant,
                status=parent.status,
                description=f"Auto-created from {parent.prefix}",
            )

            self.log_success(f"Created subnet {subnet_str}")
            created += 1

        self.log_info(f"Total subnets created: {created}")
