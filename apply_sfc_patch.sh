#!/bin/sh

VERSION="3.0.1"

PATCH_PATH=$1
PATCH_PORT=$2

Usage () {
        echo -e "\n\nUsage: apply_sfc_patch.sh <path to directory containing patches> <br-int patch port>\n"
        echo -e "\nThe patch SHOULD belong to version $VERSION and should be "
        echo -e "applied ONLY on top of sfc rpm with version $VERSION.\n"
}

# diff -Nur -x "*.py?" networking_sfc/db/ Projects/networking-sfc/networking_sfc/db/ > sfc_db-3.0.1.patch
# diff -Nur -x "*.py?" networking_sfc/services/sfc/agent/extensions/oc Projects/networking-sfc/networking_sfc/services/sfc/agent/extensions/oc > sfc_agent-3.0.1.patch
# 

function backup_files_to_be_modifed(){
    echo "Backing up files that are to be modified while patching with $1."
    if [[ $1 == "sfc_db.patch" ]]; then
        cp $PYTHON_MODULE_PATH/networking_sfc/db/sfc_db.py $PYTHON_MODULE_PATH/networking_sfc/db/sfc_db.py.bk
        cp $PYTHON_MODULE_PATH/networking_sfc/db/flowclassifier_db.py $PYTHON_MODULE_PATH/networking_sfc/db/flowclassifier_db.py.bk
    elif [[ $1 == "sfc_agent.patch" ]]; then
        cp $PYTHON_MODULE_PATH/networking_sfc/services/sfc/agent/extensions/oc/sfc_driver.py $PYTHON_MODULE_PATH/networking_sfc/services/sfc/agent/extensions/oc/sfc_driver.py.bk
    elif [[ $1 == "sfc_plugin.patch" ]]; then
        cp $PYTHON_MODULE_PATH/networking_sfc/services/sfc/drivers/oc/driver.py $PYTHON_MODULE_PATH/networking_sfc/services/sfc/drivers/oc/driver.py.bk
    else
        :
    fi
}

function apply_patches(){
    cd $PYTHON_MODULE_PATH
    for patch in ${PATCH_ARRAY[@]}
    do
        if [[ $patch != *"$VERSION"* ]]; then
            echo -e "\nThe patches must be of version $VERSION."
            exit 1
        fi
        test_patch=`patch -R -p0 --dry-run --silent > /dev/null < $patch`
        patch_status=`echo $?`
        if [ $patch_status == 0 ]; then
            echo "$patch is already applied so skipping."
        else
            backup_files_to_be_modifed $patch
            echo "Applying patch $patch ..."
            patch -p0 < $patch
            sleep 2
        fi
    done
}

function check_for_sfc(){
    sfc_rpm="$(rpm -qa | grep networking-sfc)"
    if [[ -z $sfc_rpm ]]; then
        echo -e "\nSFC RPM is missing in this node. This script works ONLY if a valid SFC rpm is installed. Aborting..."
        exit 1
    fi

    if [[ $sfc_rpm != *"$VERSION"* ]]; then
        echo -e "\nThis script works ONLY on SFC RPM installed with version $VERSION."
        exit 1
    fi
}

function copy_sfc_patches(){
    cp $PATCH_PATH/sfc/sfc_plugin.patch $PYTHON_MODULE_PATH/.
    cp $PATCH_PATH/sfc/sfc_agent.patch $PYTHON_MODULE_PATH/.
    cp $PATCH_PATH/sfc/sfc_db.patch $PYTHON_MODULE_PATH/.
}

function configure_sfc(){
    NEUTRON_CONF="/etc/neutron/neutron.conf"
    OVS_INI="/etc/neutron/plugins/ml2/openvswitch_agent.ini"
    EGG_FILE="/usr/lib/python2.7/site-packages/networking_sfc-*egg*/entry_points.txt"
    crudini --set --verbose $NEUTRON_CONF sfc drivers oc
    crudini --set --verbose $NEUTRON_CONF flowclassifier drivers oc
    if [[ `crudini --get neutron.conf DEFAULT service_plugins` != *"trunk"* ]]; then
        crudini --set --verbose $NEUTRON_CONF DEFAULT service_plugins `crudini --get neutron.conf DEFAULT service_plugins`,trunk
    fi
    crudini --set --verbose $OVS_INI ovs phy_patch_ofport $PATCH_PORT
    crudini --set --verbose $EGG_FILE networking_sfc.flowclassifier.drivers oc networking_sfc.services.flowclassifier.drivers.oc.driver:OCFlowClassifierDriver
    crudini --set --verbose $EGG_FILE networking_sfc.sfc.agent_drivers oc networking_sfc.services.sfc.agent.extensions.oc.sfc_driver:SfcOCAgentDriver
    crudini --set --verbose $EGG_FILE networking_sfc.sfc.drivers oc networking_sfc.services.sfc.drivers.oc.driver:OCSfcDriver
    
    systemctl restart neutron-server.service
    systemctl restart neutron-openvswitch-agent.service
}

function install_patch(){
    check_for_sfc
    copy_sfc_patches
    apply_patches
    configure_sfc
}

if [[ -z $PATCH_PATH ]]; then
    Usage
    exit 1
fi
if [[ -z $PATCH_PORT ]]; then
    Usage
    echo -e "Please give the patch port of br-int that interfaces the switch "
    echo -e "connected to the extreme switch. Choose the correct one from the following:\n"
    ovs-vsctl list-ports br-int
    echo -e "\nIt maybe something like int-br-vlan or int-extreme-br or the like"
    exit 1
fi

PYTHON_MODULE_PATH="/usr/lib/python2.7/dist-packages"
PATCH_ARRAY=("sfc_agent-$VERSION.patch" "sfc_plugin-$VERSION.patch" "sfc_db-$VERSION.patch")
install_patch
