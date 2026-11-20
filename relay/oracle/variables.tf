variable "compartment_id" {
  description = "OCID of the OCI compartment (use the root compartment of your tenancy)"
  type        = string
}

variable "region" {
  description = "OCI region"
  type        = string
  default     = "eu-paris-1"
}

variable "availability_domain_index" {
  description = "Availability domain index (0, 1 or 2). Change it if the ARM capacity is exhausted."
  type        = number
  default     = 0
}

variable "ssh_public_key" {
  description = "SSH public key for admin access to the VM (user ubuntu)"
  type        = string
}

variable "admin_ssh_private_key_path" {
  description = "Path to the private key matching ssh_public_key, used by Terraform to run relay-setup.sh on the VM"
  type        = string
}

# Same rules as validate_tenants in relay/relay-setup.sh.
variable "tenants" {
  description = "One entry per Home Assistant: name => { domain, port (18000-18999), public_key (shown by the integration) }"
  type = map(object({
    domain     = string
    port       = number
    public_key = string
  }))

  validation {
    condition     = length(var.tenants) > 0
    error_message = "At least one tenant is required: relay-setup.sh refuses an empty list, which would remove every relay user."
  }

  validation {
    condition     = alltrue([for name in keys(var.tenants) : can(regex("^[a-z0-9-]{1,24}$", name))])
    error_message = "Tenant names must match [a-z0-9-]{1,24}."
  }

  validation {
    condition     = alltrue([for t in values(var.tenants) : floor(t.port) == t.port && t.port >= 18000 && t.port <= 18999])
    error_message = "Tenant ports must be integers in 18000-18999."
  }

  validation {
    condition     = length(distinct([for t in values(var.tenants) : t.port])) == length(var.tenants)
    error_message = "Tenant ports must be unique."
  }

  validation {
    condition     = alltrue([for t in values(var.tenants) : can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", t.domain))])
    error_message = "Tenant domains must be lowercase DNS names, for example alice.duckdns.org."
  }

  validation {
    condition     = length(distinct([for t in values(var.tenants) : t.domain])) == length(var.tenants)
    error_message = "Tenant domains must be unique."
  }

  validation {
    condition     = alltrue([for t in values(var.tenants) : can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [!-~][ -~]*)?$", t.public_key))])
    error_message = "Tenant public keys must be single-line ssh-ed25519 keys (ssh-ed25519 AAAA...)."
  }
}

variable "remove_legacy" {
  description = "Run relay-setup.sh with --remove-legacy: lock the old ha-tunnel user, stop its sessions and give port 8123 to nginx. The user and its keys are kept."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Email address for the cost alerts (alert on any charge)"
  type        = string
}

variable "vm_shape" {
  description = "Always Free VM shape: VM.Standard.A1.Flex (ARM, 4 OCPU pool) or VM.Standard.E2.1.Micro (AMD x86, usually available)"
  type        = string
  default     = "VM.Standard.A1.Flex"

  validation {
    condition     = contains(["VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"], var.vm_shape)
    error_message = "The shape must be VM.Standard.A1.Flex or VM.Standard.E2.1.Micro (Always Free only)."
  }
}
