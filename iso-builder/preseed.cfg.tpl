# Debian Preseed - Unattended Install
# Template variables: {{IP}}, {{NETMASK}}, {{GATEWAY}}, {{DNS}},
#                     {{HOSTNAME}}, {{USER}}, {{PASSWORD_HASH}}, {{SSH_KEY}}

### Locale & Keyboard
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i console-setup/ask_detect boolean false

### Network (static)
d-i netcfg/choose_interface select auto
d-i netcfg/disable_autoconfig boolean true
d-i netcfg/get_ipaddress string {{IP}}
d-i netcfg/get_netmask string {{NETMASK}}
d-i netcfg/get_gateway string {{GATEWAY}}
d-i netcfg/get_nameservers string {{DNS}}
d-i netcfg/confirm_static boolean true
d-i netcfg/get_hostname string {{HOSTNAME}}
d-i netcfg/get_domain string local

### Clock
d-i clock-setup/utc boolean true
d-i time/zone string Europe/Berlin
d-i clock-setup/ntp boolean true

### User + Root
d-i passwd/root-login boolean true
d-i passwd/root-password-crypted string {{PASSWORD_HASH}}
d-i passwd/user-fullname string {{USER}}
d-i passwd/username string {{USER}}
d-i passwd/user-password-crypted string {{PASSWORD_HASH}}

### Partitioning (auto LVM)
d-i partman-auto/method string lvm
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-lvm/confirm boolean true
d-i partman-lvm/confirm_nooverwrite boolean true
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true

### Mirror
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string

### Packages
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string sudo qemu-guest-agent curl wget ca-certificates gnupg openssh-server
d-i pkgsel/upgrade select full-upgrade
popularity-contest popularity-contest/participate boolean false

### Grub
d-i grub-installer/only_debian boolean true
d-i grub-installer/bootdev string default

### Late commands - SSH Key, Sudo, Locale
d-i preseed/late_command string \
  in-target mkdir -p /home/{{USER}}/.ssh; \
  echo '{{SSH_KEY}}' > /target/home/{{USER}}/.ssh/authorized_keys; \
  in-target chmod 700 /home/{{USER}}/.ssh; \
  in-target chmod 600 /home/{{USER}}/.ssh/authorized_keys; \
  in-target chown -R {{USER}}:{{USER}} /home/{{USER}}/.ssh; \
  echo '{{USER}} ALL=(ALL) NOPASSWD:ALL' > /target/etc/sudoers.d/{{USER}}; \
  in-target chmod 440 /etc/sudoers.d/{{USER}}; \
  in-target systemctl enable ssh; \
  in-target systemctl enable qemu-guest-agent;

### Finish
d-i finish-install/reboot_in_progress note
d-i cdrom-detect/eject boolean true
