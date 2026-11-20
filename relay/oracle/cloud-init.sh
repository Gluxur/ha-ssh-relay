#!/bin/bash
# First boot only: packages. The relay configuration (users, sshd, nginx,
# firewall) is done by relay-setup.sh, run by terraform_data.relay_config.
set -eu

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx libnginx-mod-stream iptables-persistent jq
