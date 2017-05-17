#!/bin/sh

########################## FILL THIS INFO #####################################
# Note: Fill with hostnames. Blank, if compute does not exist.
computeA=node-204.localdomain
computeB=compute-5
computeC=compute-210

# Note: Fill with SF and client glance image names.
sf_image=sf
client_image=client
###############################################################################



if [[ -z $computeA && -z $computeB && -z $computeC ]]; then
    echo "Fill the compute node details."
    exit
fi


sf_id=`glance image-list | grep " $sf_image " | awk '{print $2}'`
client_id=`glance image-list | grep " $client_image " | awk '{print $2}'`
if [[ -z $sf_id || -z $client_id ]]; then
    echo "Fill in valid glance image names for both SF and Client VMs."
    exit
fi

operation=$1
if [ "$operation" != "create" ] && [ "$operation" != "delete" ]; then
    echo "This script expects a case-sensitive argument --> create/delete"
    exit
fi 

if [ ! -z $computeC ] ; then
    c2_compute=$computeC
    c1_compute=$computeA
    sf_compute=$computeB
elif [ ! -z $computeB ] ; then
    sf_compute=$computeB
    c2_compute=$computeA
    c1_compute=$computeA
else
    sf_compute=$computeA
    c2_compute=$computeA
    c1_compute=$computeA
fi

if [ "$operation" == "create" ]; then
    echo -e "\nCreating network Inspected_net"
    openstack network create inspected_net
    openstack subnet create --subnet-range 11.0.0.0/24 --network inspected_net inspected_subnet
    sleep 50
    echo -e "\nCreating network Inspection_net"
    openstack network create inspection_net
    openstack subnet create --subnet-range 12.0.0.0/24 --network inspection_net inspection_subnet
    sleep 50
    #openstack router create sfc-router
    
    #openstack router add subnet sfc-router inspected_subnet
    #openstack router add subnet sfc-router inspection_subnet
    
    openstack port create --network inspected_net c1
    openstack port create --network inspected_net c2
    openstack port create --network inspected_net c3
    openstack port create --network inspection_net p1
    openstack port create --network inspection_net p2
    
    p1_id=`openstack port list -f value | grep " p1 " | awk '{print $1}'`
    p2_id=`openstack port list -f value | grep " p2 " | awk '{print $1}'`
    p1_mac=`openstack port list -f value | grep " p1 " | awk '{print $3}'`
    p2_mac=`openstack port list -f value | grep " p2 " | awk '{print $3}'`
    
    c1_id=`openstack port list -f value | grep " c1 " | awk '{print $1}'`
    c2_id=`openstack port list -f value | grep " c2 " | awk '{print $1}'`
    c3_id=`openstack port list -f value | grep " c3 " | awk '{print $1}'`
   
    echo -e "\nCreating Neutron Trunk port" 
    openstack network trunk create --parent-port $p1_id trunk1
    
    echo -e "\nLaunching VNF on Node $sf_compute..."
    nova boot --image $sf_image --flavor 3 --nic port-id=$p1_id\
     --nic port-id=$p2_id --availability-zone nova:$sf_compute VNF
    
    sleep 5
    
    echo -e "\nLaunching Client1 on Node $c1_compute..."
    nova boot --image $client_image --flavor 3 --nic port-id=$c1_id  --availability-zone nova:$c1_compute Client1
    echo -e "\nLaunching Client2 on Node $sf_compute..."
    nova boot --image $client_image --flavor 3 --nic port-id=$c3_id  --availability-zone nova:$sf_compute Client2
    echo -e "\nLaunching Server on Node $c2_compute..."
    nova boot --image $client_image --flavor 3 --nic port-id=$c2_id  --availability-zone nova:$c2_compute Server
    
    sleep 5
    
    vnf_id=`openstack server list | grep VNF | awk '{print $2}'`
        
    echo -e "\nCreating a shadow port s1" 
    openstack port create --network inspected_net --mac-address $p1_mac s1
    s1_id=`openstack port list -f value | grep " s1 " | awk '{print $1}'`
    seg_id=`openstack network show inspected_net | grep segmentation_id | awk '{print $4}'`
    
    echo -e "\nCreating a shadow port s2" 
    openstack port create --network inspected_net --device $p2_id --mac-address $p2_mac s2
    
    openstack network trunk set --subport port=$s1_id,segmentation-type=vlan,segmentation-id=$seg_id trunk1
    
    openstack port set --device $p1_id s1
    
    sleep 3
    
    echo -e "\nCreating SFC Port Pair pp1."
    openstack port pair create --ingress s1 --egress s2 pp1
    echo -e "\nCreating SFC Port Pair Group ppg1."
    openstack port pair group create --port-pair pp1 ppg1 
    echo -e "\nCreating SFC Flow Classifier fc1."
    openstack flow classifier create --protocol TCP  --logical-source-port c1 --logical-destination-port c2 fc1
    echo -e "\nCreating SFC Flow Classifier fc2."
    openstack flow classifier create --protocol TCP  --logical-source-port c3 --logical-destination-port c2 fc2
    
    while true; do
        read -p "Create or delete the port chain? (create/delete/exit)  " op
        case $op in
            create ) echo -e "\nCreating SFC Port Chain pc1" && openstack port chain create --port-pair-group ppg1 --flow-classifier fc1 --flow-classifier fc2 pc1;;
            delete ) echo -e "\nDeleting SFC Port Chain pc1" && openstack port chain delete pc1;;
            exit ) exit;;
            * ) echo "Please choose either of (create/delete/exit).";;
        esac
    done

else
    openstack port chain delete pc1
    openstack flow classifier delete fc1
    openstack flow classifier delete fc2
    openstack port pair group delete ppg1
    openstack port pair delete pp1
    
    s1_id=`openstack port list -f value | grep " s1 " | awk '{print $1}'`
    openstack network trunk unset --subport $s1_id trunk1
    
    openstack server delete Client1 Server VNF Client2
    
    openstack network trunk delete trunk1
     
    openstack port delete c1 c2 p1 p2 c3 s1 s2
    
    #openstack router remove subnet sfc-router inspected_subnet
    #openstack router remove subnet sfc-router inspection_subnet
    #openstack router delete sfc-router
    openstack network delete inspected_net inspection_net 
fi

