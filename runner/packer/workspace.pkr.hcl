# Packer template for building the QEMU/KVM workspace base image.
#
# Produces a QCOW2 image at /var/lib/opencuria/images/workspace-base.qcow2
# with an identical software stack to the Docker workspace container.
#
# Usage:
#   packer init workspace.pkr.hcl
#   packer build workspace.pkr.hcl

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "ubuntu_iso_url" {
  type    = string
  default = "https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64.img"
}

variable "ubuntu_iso_checksum" {
  type    = string
  default = "none"
}

variable "disk_size" {
  type    = string
  default = "20G"
}

variable "memory" {
  type    = number
  default = 4096
}

variable "cpus" {
  type    = number
  default = 2
}

variable "ssh_username" {
  type    = string
  default = "ubuntu"
}

variable "ssh_password" {
  type    = string
  default = "packer"
}

variable "output_directory" {
  type    = string
  default = "output"
}

source "qemu" "workspace" {
  iso_url      = var.ubuntu_iso_url
  iso_checksum = var.ubuntu_iso_checksum

  disk_image       = true
  disk_size        = var.disk_size
  format           = "qcow2"
  accelerator      = "kvm"
  headless         = true
  use_default_display = false

  memory   = var.memory
  cpus     = var.cpus

  ssh_username         = var.ssh_username
  ssh_password         = var.ssh_password
  ssh_timeout          = "15m"
  ssh_handshake_attempts = 100
  shutdown_command      = "echo 'packer' | sudo -S shutdown -P now"

  output_directory = var.output_directory

  # cloud-init seed via HTTP
  http_directory = "http"
  cd_files       = ["http/meta-data", "http/user-data"]
  cd_label       = "cidata"

  qemuargs = [
    ["-cpu", "host"],
    ["-smp", "${var.cpus}"],
    ["-m", "${var.memory}"],
  ]
}

build {
  sources = ["source.qemu.workspace"]

  # Wait for cloud-init to finish before proceeding
  provisioner "shell" {
    inline = [
      "echo 'Waiting for cloud-init to finish...'",
      "cloud-init status --wait",
      "echo 'Cloud-init finished'",
    ]
    timeout = "15m"
  }

  # Copy the init script
  provisioner "file" {
    source      = "init.sh"
    destination = "/tmp/init.sh"
  }

  # Run the init script
  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S env {{ .Vars }} {{ .Path }}"
    inline = [
      "chmod +x /tmp/init.sh",
      "bash /tmp/init.sh",
    ]
    environment_vars = [
      "DEBIAN_FRONTEND=noninteractive",
    ]
  }

  # Create workspace directory and copy global agent instruction files
  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S env {{ .Vars }} {{ .Path }}"
    inline          = ["mkdir -p /workspace"]
  }

  provisioner "file" {
    source      = "../defaults/AGENTS.md"
    destination = "/tmp/AGENTS.md"
  }

  provisioner "file" {
    source      = "../defaults/CLAUDE.md"
    destination = "/tmp/CLAUDE.md"
  }

  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S env {{ .Vars }} {{ .Path }}"
    inline = [
      "mv /tmp/AGENTS.md /workspace/AGENTS.md",
      "mv /tmp/CLAUDE.md /workspace/CLAUDE.md",
    ]
  }

  # Install qemu-guest-agent for fsFreeze support
  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S env {{ .Vars }} {{ .Path }}"
    inline = [
      "apt-get update",
      "apt-get install -y qemu-guest-agent",
      "systemctl enable qemu-guest-agent",
      "rm -rf /var/lib/apt/lists/*",
    ]
  }

  # Clean up
  provisioner "shell" {
    execute_command = "echo 'packer' | sudo -S env {{ .Vars }} {{ .Path }}"
    inline = [
      "rm -f /tmp/init.sh",
      "apt-get clean",
      "rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*",
      "cloud-init clean --logs",
      "fstrim -av || true",
    ]
  }

  post-processor "shell-local" {
    inline = [
      "mv ${var.output_directory}/packer-workspace ${var.output_directory}/workspace-base.qcow2",
      "echo 'Base image built at ${var.output_directory}/workspace-base.qcow2'",
      "echo 'To install, run: sudo mkdir -p /var/lib/opencuria/images && sudo cp ${var.output_directory}/workspace-base.qcow2 /var/lib/opencuria/images/'",
    ]
  }
}
