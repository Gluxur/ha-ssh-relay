output "reserved_public_ip" {
  description = "Reserved public IP: point every tenant domain at it"
  value       = oci_core_public_ip.relay.ip_address
}

output "ssh_admin_command" {
  description = "SSH command for admin access to the VM"
  value       = "ssh -i ${var.admin_ssh_private_key_path} ubuntu@${oci_core_public_ip.relay.ip_address}"
}

output "tenants" {
  description = "Per tenant: the domain to point at the reserved IP, and the SSH user and remote port the integration uses"
  value = {
    for name, t in var.tenants : name => {
      domain      = t.domain
      ssh_user    = "relay-${name}"
      remote_port = t.port
    }
  }
}
