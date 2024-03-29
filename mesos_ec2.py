#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import with_statement

import boto
from boto.vpc import VPCConnection
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib2
from optparse import OptionParser
from sys import stderr
from boto.ec2.blockdevicemapping import BlockDeviceMapping, EBSBlockDeviceType


# A static URL from which to figure out the latest Mesos EC2 AMI
LATEST_AMI_URL = "https://s3.amazonaws.com/mesos-images/ids/latest"

def gen_user_data():
  from email.mime.multipart import MIMEMultipart
  from email.mime.text import MIMEText

  user_data = MIMEMultipart()

  config = MIMEText(open('cloud-config.yml', 'rb').read(), _subtype='cloud-config')
  config.add_header('Content-Disposition', 'attachment', filename='cloud-config.yml')
  user_data.attach(config)

  script = MIMEText(open('config.sh', 'rb').read(), _subtype='x-shellscript')
  script.add_header('Content-Disposition', 'attachment', filename='config.sh')
  user_data.attach(script)
  return user_data


def detect_masters(conn):
    

# Configure and parse our command-line arguments
def parse_args():
  parser = OptionParser(usage="mesos-ec2 [options] <action> <cluster_name>"
      + "\n\n<action> can be: launch, destroy, login, stop, start, get-master",
      add_help_option=False)
  parser.add_option("-h", "--help", action="help",
                    help="Show this help message and exit")
  parser.add_option("-s", "--slaves", type="int", default=1,
      help="Number of slaves to launch (default: 1)")
  parser.add_option("-w", "--wait", type="int", default=60,
      help="Number of seconds to wait for cluster nodes to start (default: 60)")
  parser.add_option("-k", "--key-pair",
      help="Key pair to use on instances")
  parser.add_option("-i", "--identity-file", 
      help="SSH private key file to use for logging into instances")
  parser.add_option("-t", "--instance-type", default="m1.large",
      help="Type of instance to launch (default: m1.large). " +
           "WARNING: must be 64 bit, thus small instances won't work")
  parser.add_option("-m", "--master-instance-type", default="",
      help="Master instance type (leave empty for same as instance-type)")
  parser.add_option("-z", "--zone", default="ap-southeast-2a",
      help="Availability zone to launch instances in")
  parser.add_option("-a", "--ami", default="ami-4517dc2c",
      help="Amazon Machine Image ID to use, or 'latest' to use latest " +
           "availabe AMI (default: ami-4517dc2c)")
  parser.add_option("-o", "--os", default="amazon64",
      help="OS on the Amazon Machine Image (default: amazon64)")
  parser.add_option("-d", "--download", metavar="SOURCE", default="none",
      help="Where to download latest code from: set to 'git' to check out " +
           "from git, or 'none' to use the Mesos on the AMI (default)")
  parser.add_option("-b", "--branch", default="master",
      help="If using git, which branch to check out. Default is 'master'")
  parser.add_option("-D", metavar="[ADDRESS:]PORT", dest="proxy_port", 
      help="Use SSH dynamic port forwarding to create a SOCKS proxy at " +
            "the given local address (for use with login)")
  parser.add_option("--resume", action="store_true", default=False,
      help="Resume installation on a previously launched cluster " +
           "(for debugging)")
  parser.add_option("-f", "--ft", metavar="NUM_MASTERS", default="1", 
      help="Number of masters to run. Default is 1. Greater values " + 
           "make Mesos run in fault-tolerant mode with ZooKeeper.")
  parser.add_option("--ebs-vol-size", metavar="SIZE", type="int", default=0,
      help="Attach a new EBS volume of size SIZE (in GB) to each node as " +
           "/vol. The volumes will be deleted when the instances terminate. " +
           "Only possible on EBS-backed AMIs.")
  parser.add_option("--swap", metavar="SWAP", type="int", default=1024,
      help="Swap space to set up per node, in MB (default: 1024)")
  parser.add_option("--spot-price", metavar="PRICE", type="float",
      help="If specified, launch slaves as spot instances with the given " +
            "maximum price (in dollars)")
  (opts, args) = parser.parse_args()
  opts.ft = int(opts.ft)
  if len(args) != 2:
    parser.print_help()
    sys.exit(1)
  (action, cluster_name) = args
  if opts.identity_file == None and action in ['launch', 'login']:
    print >> stderr, ("ERROR: The -i or --identity-file argument is " +
                      "required for " + action)
    sys.exit(1)
  if os.getenv('AWS_ACCESS_KEY_ID') == None:
    print >> stderr, ("ERROR: The environment variable AWS_ACCESS_KEY_ID " +
                      "must be set")
    sys.exit(1)
  if os.getenv('AWS_SECRET_ACCESS_KEY') == None:
    print >> stderr, ("ERROR: The environment variable AWS_SECRET_ACCESS_KEY " +
                      "must be set")
    sys.exit(1)
  return (opts, action, cluster_name)


# Get the EC2 security group of the given name, creating it if it doesn't exist
def get_or_make_group(conn, cluster, vpc_id, name):
  groups = conn.ec2.get_all_security_groups()
  group = [g for g in groups if g.name == name]
  if len(group) > 0:
    return group[0]
  else:
    print "Creating security group " + name
    sg = conn.ec2.create_security_group(name, "Mesos EC2 group", vpc_id=vpc_id)
    conn.ec2.create_tags([sg.id], {'MesosCluster': cluster})
    return sg


# Wait for a set of launched instances to exit the "pending" state
# (i.e. either to start running or to fail and be terminated)
def wait_for_instances(conn, instances):
  while True:
    for i in instances:
      i.update()
    if len([i for i in instances if i.state == 'pending']) > 0:
      time.sleep(5)
    else:
      return


# Check whether a given EC2 instance object is in a state we consider active,
# i.e. not terminating or terminated. We count both stopping and stopped as
# active since we can restart stopped clusters.
def is_active(instance):
  return (instance.state in ['pending', 'running', 'stopping', 'stopped'])


def get_or_make_vpc(conn, cluster_name, name):
    vpc = get_vpc(conn, cluster_name, name)

    if vpc is None:
      vpc = conn.vpc.create_vpc('172.20.0.0/16')
      wait_for_instances(conn, [vpc])
    
    conn.vpc.modify_vpc_attribute(vpc.id, enable_dns_support=True)
    conn.vpc.modify_vpc_attribute(vpc.id, enable_dns_hostnames=True)
    conn.ec2.create_tags([vpc.id], {
      'Name':name,
      'MesosCluster':cluster_name,
      })
    return vpc


def get_vpc(conn, cluster_name, name):
  vpcs = conn.vpc.get_all_vpcs(filters={
        'tag:MesosCluster': cluster_name,
        'tag:Name': name
        })
  vpc = None
  if len(vpcs) > 0:
    vpc = vpcs[0]
  return vpc

def get_or_make_subnet(conn, vpc_id, zone, cluster_name, name):
  subnets = conn.vpc.get_all_subnets(filters={
    'vpcId': vpc_id,
    'availabilityZone': zone
    })
  subnet = None
  if len(subnets) > 0:
    subnet = subnets[0]
  else:
    subnet = conn.vpc.create_subnet(vpc_id, '172.20.0.0/24', availability_zone=zone)

  conn.ec2.create_tags([subnet.id], {
    'Name':name,
    'MesosCluster':cluster_name,
    })
  return subnet


class AWSConnection:
  def __init__(self, ec2, vpc):
    self.ec2 = ec2
    self.vpc = vpc

def get_or_make_ig(conn, vpc_id, cluster_name, name):
  igs = conn.vpc.get_all_internet_gateways(filters={
    'attachment.vpc-id': vpc_id
    })
  ig = None
  if len(igs) > 0:
    ig = igs[0]
  else:
    ig = conn.vpc.create_internet_gateway()
    conn.vpc.attach_internet_gateway(ig.id, vpc_id)

  conn.ec2.create_tags([ig.id], {
    'Name': name,
    'MesosCluster': cluster_name
    })
  return ig

def get_or_make_rt(conn, vpc_id, cluster_name, name):
  rts = conn.vpc.get_all_route_tables(filters={
      'vpc-id': vpc_id
    })
  rt = None
  if len(rts) > 0:
    rt = rts[0]
  else:
    rt = conn.vpc.create_route_table(vpc_id)

  conn.ec2.create_tags([rt.id], {
    'Name': name,
    'MesosCluster': cluster_name
  })
  return rt

# Launch a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master, slave
# and zookeeper instances (in that order).
# Fails if there already instances running in the cluster's groups.
def launch_cluster(conn, opts, cluster_name):

  conn = AWSConnection(conn, VPCConnection(region=conn.region))

  print "Setting up VPC..."
  vpc = get_or_make_vpc(conn, cluster_name, 'mesos-vpc')
  print "Using vpc: %s" % (vpc.id)


  print "Setting up subnet..."
  subnet = get_or_make_subnet(conn, vpc.id, opts.zone, cluster_name, 'mesos-subnet')
  print "Using subnet: %s" % (subnet.id)

  # Add internet gateway to VPC.
  print "Creating internet gateway"
  ig = get_or_make_ig(conn, vpc.id, cluster_name, 'mesos-vpc')
  print "Using internet gateway: %s" % (ig.id)
  
  # Add route to route table
  rt = get_or_make_rt(conn, vpc.id, cluster_name, 'mesos-rt')
  conn.vpc.create_route(rt.id, '0.0.0.0/0', gateway_id=ig.id)

  print "Setting up security groups..."
  master_group = get_or_make_group(conn, cluster_name, vpc.id, "mesos-masters")
  slave_group = get_or_make_group(conn, cluster_name, vpc.id, "mesos-slaves")
  zoo_group = get_or_make_group(conn, cluster_name, vpc.id, "mesos-zoo")

  if master_group.rules == []: # Group was just now created
    master_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    master_group.authorize('tcp', 8080, 8081, '0.0.0.0/0')
    master_group.authorize('tcp', 50030, 50030, '0.0.0.0/0')
    master_group.authorize('tcp', 50070, 50070, '0.0.0.0/0')
    master_group.authorize('tcp', 60070, 60070, '0.0.0.0/0')
    master_group.authorize('tcp', 38090, 38090, '0.0.0.0/0')
  if slave_group.rules == []: # Group was just now created
    slave_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    slave_group.authorize('tcp', 8080, 8081, '0.0.0.0/0')
    slave_group.authorize('tcp', 50060, 50060, '0.0.0.0/0')
    slave_group.authorize('tcp', 50075, 50075, '0.0.0.0/0')
    slave_group.authorize('tcp', 60060, 60060, '0.0.0.0/0')
    slave_group.authorize('tcp', 60075, 60075, '0.0.0.0/0')
  if zoo_group.rules == []: # Group was just now created
    zoo_group.authorize('tcp', 22, 22, '0.0.0.0/0')
    zoo_group.authorize('tcp', 2181, 2181, '0.0.0.0/0')
    zoo_group.authorize('tcp', 2888, 2888, '0.0.0.0/0')
    zoo_group.authorize('tcp', 3888, 3888, '0.0.0.0/0')

  # Check if instances are already running in our groups
  print "Checking for running cluster..."
  reservations = conn.ec2.get_all_instances()
  for res in reservations:
    group_names = [g.name for g in res.groups]
    if master_group.name in group_names or slave_group.name in group_names or zoo_group.name in group_names:
      active = [i for i in res.instances if is_active(i)]
      if len(active) > 0:
        print >> stderr, ("ERROR: There are already instances running in " +
            "group %s, %s or %s" % (master_group.name, slave_group.name, zoo_group.name))
        sys.exit(1)
  
  print "Launching instances..."
  if opts.ami == "latest":
    # Figure out the latest AMI from our static URL
    try:
      opts.ami = urllib2.urlopen(LATEST_AMI_URL).read().strip()
    except:
      print >> stderr, "Could not read " + LATEST_AMI_URL

  try:
    image = conn.ec2.get_all_images(image_ids=[opts.ami])[0]
  except:
    print >> stderr, "Could not find AMI " + opts.ami
    sys.exit(1)

  # Create block device mapping so that we can add an EBS volume if asked to
  block_map = BlockDeviceMapping()
  if opts.ebs_vol_size > 0:
    device = EBSBlockDeviceType()
    device.size = opts.ebs_vol_size
    device.delete_on_termination = True
    block_map["/dev/sdv"] = device

  # Launch slaves
  if opts.spot_price != None:
    # Launch spot instances with the requested price
    print ("Requesting %d slaves as spot instances with price $%.3f" %
           (opts.slaves, opts.spot_price))
    slave_reqs = conn.ec2.request_spot_instances(
        price = opts.spot_price,
        image_id = opts.ami,
        launch_group = "launch-group-%s" % cluster_name,
        placement = opts.zone,
        count = opts.slaves,
        key_name = opts.key_pair,
        security_groups = [slave_group],
        instance_type = opts.instance_type,
        block_device_map = block_map)
    my_req_ids = [req.id for req in slave_reqs]
    print "Waiting for spot instances to be granted..."
    while True:
      time.sleep(10)
      reqs = conn.get_all_spot_instance_requests()
      id_to_req = {}
      for r in reqs:
        id_to_req[r.id] = r
      active = 0
      instance_ids = []
      for i in my_req_ids:
        if id_to_req[i].state == "active":
          active += 1
          instance_ids.append(id_to_req[i].instance_id)
      if active == opts.slaves:
        print "All %d slaves granted" % opts.slaves
        reservations = conn.get_all_instances(instance_ids)
        slave_nodes = []
        for r in reservations:
          slave_nodes += r.instances
        break
      else:
        print "%d of %d slaves granted, waiting longer" % (active, opts.slaves)
  else:
    # Launch non-spot instances
    slave_res = conn.ec2.run_instances(opts.ami,
                          key_name = opts.key_pair,
                          subnet_id = subnet.id,
                          security_group_ids = [slave_group.id],
                          instance_type = opts.instance_type,
                          placement = opts.zone,
                          min_count = opts.slaves,
                          max_count = opts.slaves,
                          block_device_map = block_map)
    slave_nodes = slave_res.instances
    print "Launched slaves, regid = " + slave_res.id

  # Launch masters
  master_type = opts.master_instance_type
  if master_type == "":
    master_type = opts.instance_type
  interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(subnet_id=subnet.id,
                                                                    groups=[master_group.id],
                                                                    associate_public_ip_address=True)
  interfaces = boto.ec2.networkinterface.NetworkInterfaceCollection(interface)
  master_res = conn.ec2.run_instances(opts.ami,
                          key_name = opts.key_pair,
                         instance_type = master_type,
                         placement = opts.zone,
                         network_interfaces = interfaces,
                         min_count = opts.ft,
                         max_count = opts.ft,
                         block_device_map = block_map)
  master_nodes = master_res.instances
  print "Launched master, regid = " + master_res.id

  # Launch ZooKeeper nodes if required
  if opts.ft > 1:
    zoo_res = conn.ec2.run_instances(opts.ami,
                        key_name = opts.key_pair,
                        subnet_id = subnet.id,
                        security_group_ids = [zoo_group.id],
                        instance_type = opts.instance_type,
                        placement = opts.zone,
                        min_count = 3,
                        max_count = 3,
                        block_device_map = block_map)
    zoo_nodes = zoo_res.instances
    print "Launched zoo, regid = " + zoo_res.id
  else:
    zoo_nodes = []

  # Return all the instances
  return (master_nodes, slave_nodes, zoo_nodes)


# Get the EC2 instances in an existing cluster if available.
# Returns a tuple of lists of EC2 instance objects for the masters,
# slaves and zookeeper nodes (in that order).
def get_existing_cluster(conn, opts, cluster_name):
  print "Searching for existing cluster " + cluster_name + "..."
  instances = conn.get_only_instances()
  master_nodes = []
  slave_nodes = []
  zoo_nodes = []
  for instance in instances:
    groups = [g.name for g in instance.groups]
    if "mesos-masters" in groups:
      master_nodes.append(instance)
    elif "mesos-slaves" in groups:
      slave_nodes.append(instance)
    elif "mesos-zoo" in groups:
      zoo_nodes.append(instance)
  if master_nodes != [] and slave_nodes != []:
    print ("Found %d master(s), %d slaves, %d ZooKeeper nodes" %
           (len(master_nodes), len(slave_nodes), len(zoo_nodes)))
    return (master_nodes, slave_nodes, zoo_nodes)
  else:
    if master_nodes == [] and slave_nodes != []:
      print "ERROR: Could not find master in group mesos-masters"
    elif master_nodes != [] and slave_nodes == []:
      print "ERROR: Could not find slaves in group mesos-slaves"
    else:
      print "ERROR: Could not find any existing cluster"
    sys.exit(1)

# Deploy configuration files and run setup scripts on a newly launched
# or started EC2 cluster.
def setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, deploy_ssh_key):
  print "Deploying files to master..."
  deploy_files(conn, "deploy." + opts.os, opts, master_nodes, slave_nodes, zoo_nodes)
  master = master_nodes[0].public_dns_name
  if deploy_ssh_key:
    print "Copying SSH key %s to master..." % opts.identity_file
    ssh(master, opts, 'mkdir -p /root/.ssh')
    scp(master, opts, opts.identity_file, '/root/.ssh/id_rsa')
  print "Running setup on master..."
  ssh(master, opts, "chmod u+x mesos-ec2/setup")
  ssh(master, opts, "mesos-ec2/setup %s %s %s %s" %
      (opts.os, opts.download, opts.branch, opts.swap))
  print "Done!"


# Wait for a whole cluster (masters, slaves and ZooKeeper) to start up
def wait_for_cluster(conn, wait_secs, master_nodes, slave_nodes, zoo_nodes):
  print "Waiting for instances to start up..."
  time.sleep(5)
  wait_for_instances(conn, master_nodes)
  wait_for_instances(conn, slave_nodes)
  if zoo_nodes != []:
    wait_for_instances(conn, zoo_nodes)
  print "Waiting %d more seconds..." % wait_secs
  time.sleep(wait_secs)


# Get number of local disks available for a given EC2 instance type.
def get_num_disks(instance_type):
  # From http://docs.amazonwebservices.com/AWSEC2/latest/UserGuide/index.html?InstanceStorage.html
  disks_by_instance = {
    "m1.small":    1,
    "m1.large":    2,
    "m1.xlarge":   4,
    "t1.micro":    1,
    "c1.medium":   1,
    "c1.xlarge":   4,
    "m2.xlarge":   1,
    "m2.2xlarge":  1,
    "m2.4xlarge":  2,
    "cc1.4xlarge": 2,
    "cc2.8xlarge": 4,
    "cg1.4xlarge": 2
  }
  if instance_type in disks_by_instance:
    return disks_by_instance[instance_type]
  else:
    print >> stderr, ("WARNING: Don't know number of disks on instance type %s; assuming 1"
                      % instance_type)
    return 1


# Deploy the configuration file templates in a given local directory to
# a cluster, filling in any template parameters with information about the
# cluster (e.g. lists of masters and slaves). Files are only deployed to
# the first master instance in the cluster, and we expect the setup
# script to be run on that instance to copy them to other nodes.
def deploy_files(conn, root_dir, opts, master_nodes, slave_nodes, zoo_nodes):
  active_master = master_nodes[0].public_dns_name

  num_disks = get_num_disks(opts.instance_type)
  hdfs_data_dirs = "/mnt/ephemeral-hdfs/data"
  mapred_local_dirs = "/mnt/hadoop/mrlocal"
  if num_disks > 1:
    for i in range(2, num_disks + 1):
      hdfs_data_dirs += ",/mnt%d/ephemeral-hdfs/data" % i
      mapred_local_dirs += ",/mnt%d/hadoop/mrlocal" % i

  if zoo_nodes != []:
    zoo_list = '\n'.join([i.public_dns_name for i in zoo_nodes])
    cluster_url = "zoo://" + ",".join(
        ["%s:2181/mesos" % i.public_dns_name for i in zoo_nodes])
  else:
    zoo_list = "NONE"
    # TODO: temporary code to support older versions of Mesos with 1@ URLs
    if opts.os == "amazon64":
      cluster_url = "master@%s:5050" % active_master
    else:
      cluster_url = "1@%s:5050" % active_master

  template_vars = {
    "master_list": '\n'.join([i.public_dns_name for i in master_nodes]),
    "active_master": active_master,
    "slave_list": '\n'.join([i.public_dns_name for i in slave_nodes]),
    "zoo_list": zoo_list,
    "cluster_url": cluster_url,
    "hdfs_data_dirs": hdfs_data_dirs,
    "mapred_local_dirs": mapred_local_dirs
  }

  # Create a temp directory in which we will place all the files to be
  # deployed after we substitue template parameters in them
  tmp_dir = tempfile.mkdtemp()
  for path, dirs, files in os.walk(root_dir):
    if path.find(".svn") == -1:
      dest_dir = os.path.join('/', path[len(root_dir):])
      local_dir = tmp_dir + dest_dir
      if not os.path.exists(local_dir):
        os.makedirs(local_dir)
      for filename in files:
        if filename[0] not in '#.~' and filename[-1] != '~':
          dest_file = os.path.join(dest_dir, filename)
          local_file = tmp_dir + dest_file
          with open(os.path.join(path, filename)) as src:
            with open(local_file, "w") as dest:
              text = src.read()
              for key in template_vars:
                text = text.replace("{{" + key + "}}", template_vars[key])
              dest.write(text)
              dest.close()
  # rsync the whole directory over to the master machine
  command = (("rsync -rv -e 'ssh -o StrictHostKeyChecking=no -i %s' " + 
      "'%s/' 'ec2-user@%s:/'") % (opts.identity_file, tmp_dir, active_master))
  subprocess.check_call(command, shell=True)
  # Remove the temp directory we created above
  shutil.rmtree(tmp_dir)


# Copy a file to a given host through scp, throwing an exception if scp fails
def scp(host, opts, local_file, dest_file):
  subprocess.check_call(
      "scp -q -o StrictHostKeyChecking=no -i %s '%s' 'ec2-user@%s:%s'" %
      (opts.identity_file, local_file, host, dest_file), shell=True)


# Run a command on a host through ssh, throwing an exception if ssh fails
def ssh(host, opts, command):
  subprocess.check_call(
      "ssh -t -o StrictHostKeyChecking=no -i %s ec2-user@%s '%s'" %
      (opts.identity_file, host, command), shell=True)


def main():
  (opts, action, cluster_name) = parse_args()
  conn = boto.ec2.connect_to_region('ap-southeast-2')

  # Select an AZ at random if it was not specified.
  print opts.zone
  if opts.zone == "":
    opts.zone = random.choice(conn.get_all_zones()).name
    print opts.zone

  if action == "launch":
    if opts.resume:
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name)
    else:
      (master_nodes, slave_nodes, zoo_nodes) = launch_cluster(
          conn, opts, cluster_name)
      wait_for_cluster(conn, opts.wait, master_nodes, slave_nodes, zoo_nodes)
    setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, True)

  elif action == "destroy":
    response = raw_input("Are you sure you want to destroy the cluster " +
        cluster_name + "?\nALL DATA ON ALL NODES WILL BE LOST!!\n" +
        "Destroy cluster " + cluster_name + " (y/N): ")
    if response == "y":
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(conn, opts, cluster_name)
      print "Terminating master..."
      for inst in master_nodes:
        inst.terminate()
      print "Terminating slaves..."
      for inst in slave_nodes:
        inst.terminate()
      if zoo_nodes != []:
        print "Terminating zoo..."
        for inst in zoo_nodes:
          inst.terminate()

  elif action == "login":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
        conn, opts, cluster_name)
    master = master_nodes[0].public_dns_name
    print "Logging into master " + master + "..."
    proxy_opt = ""
    if opts.proxy_port != None:
      proxy_opt = "-D " + opts.proxy_port
    subprocess.check_call("ssh -o StrictHostKeyChecking=no -i %s %s ec2-user@%s" %
        (opts.identity_file, proxy_opt, master), shell=True)

  elif action == "get-master":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(conn, opts, cluster_name)
    print master_nodes[0].public_dns_name

  elif action == "stop":
    response = raw_input("Are you sure you want to stop the cluster " +
        cluster_name + "?\nDATA ON EPHEMERAL DISKS WILL BE LOST, " +
        "BUT THE CLUSTER WILL KEEP USING SPACE ON\n" + 
        "AMAZON EBS IF IT IS EBS-BACKED!!\n" +
        "Stop cluster " + cluster_name + " (y/N): ")
    if response == "y":
      (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
          conn, opts, cluster_name)
      print "Stopping master..."
      for inst in master_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.stop()
      print "Stopping slaves..."
      for inst in slave_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.stop()
      if zoo_nodes != []:
        print "Stopping zoo..."
        for inst in zoo_nodes:
          if inst.state not in ["shutting-down", "terminated"]:
            inst.stop()

  elif action == "start":
    (master_nodes, slave_nodes, zoo_nodes) = get_existing_cluster(
        conn, opts, cluster_name)
    print "Starting slaves..."
    for inst in slave_nodes:
      if inst.state not in ["shutting-down", "terminated"]:
        inst.start()
    print "Starting master..."
    for inst in master_nodes:
      if inst.state not in ["shutting-down", "terminated"]:
        inst.start()
    if zoo_nodes != []:
      print "Starting zoo..."
      for inst in zoo_nodes:
        if inst.state not in ["shutting-down", "terminated"]:
          inst.start()
    wait_for_cluster(conn, opts.wait, master_nodes, slave_nodes, zoo_nodes)
    setup_cluster(conn, master_nodes, slave_nodes, zoo_nodes, opts, False)

  elif action == "shutdown":
    print >> stderr, ("The shutdown action is no longer available.\n" +
        "Use either 'destroy' to delete a cluster and all data on it,\n" +
        "or 'stop' to shut down the machines but have them persist if\n" +
        "you launched an EBS-backed cluster.")
    sys.exit(1)

  else:
    print >> stderr, "Invalid action: %s" % action
    sys.exit(1)


if __name__ == "__main__":
  logging.basicConfig()
  main()
