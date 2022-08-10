import os
import json
import traceback
from ipaddress import ip_address, IPv4Address, IPv6Address, IPv4Network, IPv6Network
from datetime import datetime, timedelta
from dateutil import tz
import time
import sys

# Chameleon Library
import chi
import chi.lease 
from chi.server import *
from chi.lease import *
from chi.network import *

# FABRIC Library
from fabrictestbed_extensions.fablib.fablib import FablibManager as fablib_manager

chameleon_prefix =  "fabric_stitch_"
chameleon_server_name = chameleon_prefix+'Server'
chameleon_network_name = chameleon_prefix+'Net'
chameleon_subnet_name = chameleon_prefix+'Subnet'
chameleon_router_name = chameleon_prefix+'Router'
chameleon_lease_name = chameleon_prefix+'Lease'

chameleon_image_name='CC-Ubuntu20.04'
chameleon_node_type="compute_cascadelake_r"
chameleon_physical_network='physnet1'
chameleon_stitch_provider='fabric'
chameleon_server_count=1
chameleon_key_name='aessiari-chi-uc'


# Create a FABlib manager
fablib = fablib_manager()
fablib.show_config()

# FABRIC Config
fabric_slice_name='chameleon_stitch'
fabric_node_name='node1'

fabric_node_image='default_ubuntu_20'
fabric_site=fablib.get_random_site()
print(f'fabric_site: {fabric_site}')


subnet = IPv4Network("192.168.100.0/24")

fabric_allocation_pool_start=IPv4Address('192.168.100.200')
fabric_allocation_pool_end=IPv4Address('192.168.100.250')
fabric_available_ips=[]
for ip_int in range(int(fabric_allocation_pool_start),int(fabric_allocation_pool_end)+1):
    fabric_available_ips.append(IPv4Address(ip_int))
    
chameleon_allocation_pool_start='192.168.100.100'
chameleon_allocation_pool_end='192.168.100.150'
chameleon_gateway_ip='192.168.100.1'

BLAZAR_TIME_FORMAT = '%Y-%m-%d %H:%M'

# Set start/end date for lease
# Start one minute into future to avoid Blazar thinking lease is in past
# due to rounding to closest minute.
start_date = (datetime.now(tz=tz.tzutc()) + timedelta(minutes=1)).strftime(BLAZAR_TIME_FORMAT)
end_date   = (datetime.now(tz=tz.tzutc()) + timedelta(days=1)).strftime(BLAZAR_TIME_FORMAT)

# Build list of reservations (in this case there is only one reservation)
print(start_date)
print(end_date)

reservation_list = []
chi.lease.add_node_reservation(reservation_list, count=chameleon_server_count, node_type=chameleon_node_type)

reservation_list.append(
        {
            "resource_type": "network",
            "network_name": chameleon_network_name,
            "network_properties": "",
            "resource_properties": json.dumps(
                ["==", "$stitch_provider", chameleon_stitch_provider]
            ),
        }
)

print(reservation_list)
# Create the lease

chameleon_lease = chi.lease.create_lease(chameleon_lease_name,
                                  reservations=reservation_list,
                                  start_date=start_date,
                                  end_date=end_date)

if chameleon_lease:
   print(chameleon_lease)
   print(json.dumps(chameleon_lease, indent=2))


if chameleon_lease:
    chi.lease.wait_for_active(chameleon_lease_name)

    chameleon_compute_reservation_id = [reservation for reservation in chameleon_lease['reservations'] if reservation['resource_type'] == 'physical:host'][0]['id']
    chameleon_network_reservation_id = [reservation for reservation in chameleon_lease['reservations'] if reservation['resource_type'] == 'network'][0]['id']
    #chameleon_floatingip_reservation_id = [reservation for reservation in chameleon_lease['reservations'] if reservation['resource_type'] == 'virtual:floatingip'][0]['id']

    print(f"chameleon_compute_reservation_id: {chameleon_compute_reservation_id}")
    print(f"chameleon_network_reservation_id: {chameleon_network_reservation_id}")
    #print(f"chameleon_floatingip_reservation_id: {chameleon_floatingip_reservation_id}")
else:
   print("Exiting no lease")
   sys.exit(1)

network_vlan = None
while network_vlan == None:
    try:
        #Get the network
        chameleon_network = chi.network.get_network(chameleon_network_name)

        #Get the network ID
        chameleon_network_id = chameleon_network['id']
        print(f'Chameleon Network ID: {chameleon_network_id}')

        #Get the VLAN tag (needed for FABRIC stitching)
        network_vlan = chameleon_network['provider:segmentation_id']
        print(f'network_vlan: {network_vlan}')
    except:
        print(f'Chameleon Network is not ready. Trying again!')
        time.sleep(10)           

chameleon_subnet = chi.network.create_subnet(chameleon_subnet_name, chameleon_network_id, 
                                             cidr=str(subnet),
                                             allocation_pool_start=chameleon_allocation_pool_start,
                                             allocation_pool_end=chameleon_allocation_pool_end,
                                             gateway_ip=chameleon_gateway_ip)

print(json.dumps(chameleon_subnet, indent=2))

chameleon_router = chi.network.create_router(chameleon_router_name, gw_network_name='public')

print(json.dumps(chameleon_router, indent=2))


chi.network.add_subnet_to_router_by_name(chameleon_router_name, chameleon_subnet_name)


try:
    #Create a slice
    fabric_slice = fablib.new_slice(name=fabric_slice_name)
    
    fabric_node = fabric_slice.add_node(name=fabric_node_name, site=fabric_site, image=fabric_node_image)
    fabric_node_iface = fabric_node.add_component(model='NIC_ConnectX_5', name=f"nic1").get_interfaces()[0]

    fabric_facility_port = fabric_slice.add_facility_port(name='Chameleon-StarLight', site='STAR', vlan=str(network_vlan))
    fabric_facility_port_iface = fabric_facility_port.get_interfaces()[0]

    fabric_net = fabric_slice.add_l2network(name=f'net_facility_port', interfaces=[fabric_node_iface,fabric_facility_port_iface])

    #Submit the Request
    fabric_slice.submit()
except Exception as e:
    print(f"Exception: {e}")
    traceback.print_exc()

try:        
    fabric_node = fabric_slice.get_node(name=fabric_node_name)   
    
    fabric_node_iface = fabric_node.get_interface(network_name=f'net_facility_port') 
    fabric_node_addr = fabric_available_ips.pop(0)
    print(f"fabric_node_addr: {fabric_node_addr}")
    fabric_node_iface.ip_addr_add(addr=fabric_node_addr, subnet=subnet)
    
    stdout, stderr = fabric_node.execute(f'ip addr show {fabric_node_iface.get_os_interface()}')
    print (stdout)
except Exception as e:
    print(f"Exception: {e}")

try:
    fabric_node = fabric_slice.get_node(name=fabric_node_name)     
    fabric_node_iface = fabric_node.get_interface(network_name=f'net_facility_port') 


    stdout, stderr = fabric_node.execute(f'ping -c 5 {chameleon_gateway_ip}')
    print (stdout)
    print (stderr)
    
except Exception as e:
    print(f"Exception: {e}")

for i in range(chameleon_server_count):
    server_name=f"{chameleon_server_name}_{i}"
    # Create the server
    server = chi.server.create_server(server_name, 
                                  reservation_id=chameleon_compute_reservation_id, 
                                  network_name=chameleon_network_name, 
                                  image_name=chameleon_image_name,
                                  key_name=chameleon_key_name
                                 )
    # Wait until the server is active
    #chi.server.wait_for_active(server.id)


#get fixed ips
fixed_ips={}
for i in range(chameleon_server_count):
    server_name=f"{chameleon_server_name}_{i}"
    server_id = get_server_id(server_name)
    fixed_ip = get_server(server_id).interface_list()[0].to_dict()["fixed_ips"][0]["ip_address"]
    fixed_ips[server_name]=fixed_ip

for server_name,fixed_ip in fixed_ips.items():
    print(f'{server_name}: {fixed_ip}')


for server_name,fixed_ip in fixed_ips.items():
    print(f'{server_name}: {fixed_ip}')
    
    stdout, stderr = fabric_node.execute(f'ping -c 5 {fixed_ip}')
    print (stdout)
    print (stderr)

for i in range(chameleon_server_count):
    server_name=f"{chameleon_server_name}_{i}"
    chi.server.delete_server(get_server_id(server_name))

router_id = chameleon_router['id']
subnet_id = chameleon_subnet['id']

try:
    result = chi.network.remove_subnet_from_router(router_id, subnet_id)
except Exception as e:
    print(f"detach_router_by_name error: {str(e)}")
    pass

try:
    result = chi.network.delete_router(router_id)
except Exception as e:
    print(f"delete_router_by_name error: {str(e)}")
    pass

try:
    result = chi.network.delete_subnet(subnet_id)
except Exception as e:
    print(f"delete_subnet_by_name error: {str(e)}")
    pass

try:
    result = chi.network.delete_network(network_id)
except Exception as e:
    print(f"delete_network_by_name error: {str(e)}")

chi.lease.delete_lease(chameleon_lease['id'])

try:
    fabric_slice.delete()
except Exception as e:
    print(f"Exception: {e}")
