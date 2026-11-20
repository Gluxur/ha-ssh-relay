terraform {
  required_version = ">= 1.4" # terraform_data

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
}

provider "oci" {
  region = var.region
  # Auth through ~/.oci/config (created by `oci setup config`).
}

# ── Availability domains ──────────────────────────────────────────────────────

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_id
}

locals {
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index].name
}

# ── Network ───────────────────────────────────────────────────────────────────

resource "oci_core_vcn" "relay" {
  compartment_id = var.compartment_id
  cidr_blocks    = ["10.0.0.0/16"]
  display_name   = "vcn-ha-relay"
  dns_label      = "harelay"
}

resource "oci_core_internet_gateway" "relay" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.relay.id
  display_name   = "igw-ha-relay"
  enabled        = true
}

resource "oci_core_route_table" "relay" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.relay.id
  display_name   = "rt-ha-relay"

  route_rules {
    network_entity_id = oci_core_internet_gateway.relay.id
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
  }
}

resource "oci_core_security_list" "relay" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.relay.id
  display_name   = "sl-ha-relay"

  # SSH: admin access and the reverse tunnels of every Home Assistant.
  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 22
      max = 22
    }
  }

  # HTTPS: nginx, routed by SNI to the tunnel of the tenant.
  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }

  # Home Assistant port: nginx, routed by SNI to the tunnel of the tenant.
  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 8123
      max = 8123
    }
  }

  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }
}

resource "oci_core_subnet" "relay" {
  compartment_id    = var.compartment_id
  vcn_id            = oci_core_vcn.relay.id
  cidr_block        = "10.0.1.0/24"
  display_name      = "subnet-ha-relay"
  dns_label         = "relay"
  route_table_id    = oci_core_route_table.relay.id
  security_list_ids = [oci_core_security_list.relay.id]
}

# ── Ubuntu 22.04 image (ARM or x86 depending on the shape) ────────────────────

data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_id
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = var.vm_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# ── VM instance ───────────────────────────────────────────────────────────────

resource "oci_core_instance" "relay" {
  compartment_id      = var.compartment_id
  availability_domain = local.availability_domain
  display_name        = "vm-ha-relay"
  shape               = var.vm_shape

  # shape_config only for VM.Standard.A1.Flex (a flex shape).
  # VM.Standard.E2.1.Micro is a fixed shape, without OCPU/memory settings.
  dynamic "shape_config" {
    for_each = var.vm_shape == "VM.Standard.A1.Flex" ? [1] : []
    content {
      ocpus         = 1
      memory_in_gbs = 6
    }
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu.images[0].id
    boot_volume_size_in_gbs = 50
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.relay.id
    display_name     = "vnic-ha-relay"
    assign_public_ip = false # the reserved public IP is attached separately
    hostname_label   = "ha-relay"
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    # Packages only: the whole relay configuration is done by relay-setup.sh
    # (terraform_data.relay_config below).
    user_data = base64encode(file("${path.module}/cloud-init.sh"))
  }

  # cloud-init only runs on the first boot: a change of user_data or a newer
  # Ubuntu image must not recreate the VM (IP, host key, configuration).
  lifecycle {
    ignore_changes = [metadata, source_details]
  }
}

# ── Reserved public IP ────────────────────────────────────────────────────────

data "oci_core_vnic_attachments" "relay" {
  compartment_id = var.compartment_id
  instance_id    = oci_core_instance.relay.id
}

data "oci_core_vnic" "relay" {
  vnic_id = data.oci_core_vnic_attachments.relay.vnic_attachments[0].vnic_id
}

data "oci_core_private_ips" "relay" {
  vnic_id = data.oci_core_vnic.relay.id
}

resource "oci_core_public_ip" "relay" {
  compartment_id = var.compartment_id
  lifetime       = "RESERVED"
  display_name   = "pip-ha-relay"
  private_ip_id  = data.oci_core_private_ips.relay.private_ips[0].id
}

# ── Relay configuration (users, sshd, nginx, firewall) ────────────────────────
# Runs relay-setup.sh over SSH whenever the tenants, the script or
# remove_legacy change. The script is idempotent and rolls back on failure.

resource "terraform_data" "relay_config" {
  triggers_replace = [
    sha256(jsonencode(var.tenants)),
    filesha256("${path.module}/../relay-setup.sh"),
    var.remove_legacy,
  ]

  connection {
    type        = "ssh"
    host        = oci_core_public_ip.relay.ip_address
    user        = "ubuntu"
    private_key = file(var.admin_ssh_private_key_path)
  }

  provisioner "file" {
    source      = "${path.module}/../relay-setup.sh"
    destination = "/tmp/relay-setup.sh"
  }

  provisioner "file" {
    content     = jsonencode({ tenants = var.tenants })
    destination = "/tmp/tenants.json"
  }

  provisioner "remote-exec" {
    inline = [
      # First boot: let cloud-init finish its apt-get (dpkg lock) first.
      "sudo cloud-init status --wait > /dev/null || true",
      "sudo bash /tmp/relay-setup.sh ${var.remove_legacy ? "--remove-legacy " : ""}/tmp/tenants.json",
    ]
  }
}

# ── Budget guard: alert on any charge ─────────────────────────────────────────
# The budget must be created on the root compartment (tenancy).
# Two alerts: actual charges > 0.01 AND forecast charges > 0.01.

resource "oci_budget_budget" "zero_cost_guard" {
  compartment_id = var.compartment_id
  amount         = 1
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [var.compartment_id]
  display_name   = "budget-ha-relay-zero-cost"
  description    = "Always Free guard: alert as soon as any charge appears"
}

resource "oci_budget_alert_rule" "actual_cost" {
  budget_id      = oci_budget_budget.zero_cost_guard.id
  type           = "ACTUAL"
  threshold      = 0.01
  threshold_type = "ABSOLUTE"
  display_name   = "alert-ha-relay-actual-cost"
  recipients     = var.alert_email
  message        = "ALERT: Oracle Cloud charges were detected this month. The ha-relay VM may no longer be in the Always Free tier. Check now: https://cloud.oracle.com/invoices-and-orders/bills"
}

resource "oci_budget_alert_rule" "forecast_cost" {
  budget_id      = oci_budget_budget.zero_cost_guard.id
  type           = "FORECAST"
  threshold      = 0.01
  threshold_type = "ABSOLUTE"
  display_name   = "alert-ha-relay-forecast-cost"
  recipients     = var.alert_email
  message        = "FORECAST ALERT: Oracle Cloud forecasts charges this month. Check before billing: https://cloud.oracle.com/invoices-and-orders/bills"
}
