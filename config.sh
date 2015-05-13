#!/bin/bash

# Cloud init script for setting env and installing salt.

function generate-broken-motd {
  echo -e "Broken (or in progress) mesos cluster setup." > /etc/motd
}

function generate-fixed-motd() {
  echo -e '\n=== Mesos node setup complete ===\n' > /etc/motd
}

function fix-apt-sources() {
  sed -i -e "\|^deb.*http://http.debian.net/debian| s/^/#/" /etc/apt/sources.list
  sed -i -e "\|^deb.*http://ftp.debian.org/debian| s/^/#/" /etc/apt/sources.list.d/backports.list
}

function setup-masterless-salt() {
  cat <<EOF >/etc/salt/minion.d/local.conf
file_client: local
  file_roots:
    base:
      - /srv/salt
EOF
}

function setup-slave-role() {
  cat <<EOF >/etc/salt/minion.d/grains.conf
grains:
  roles:
    - mesos-slave
EOF
}

function generate-mesos-env() {
  local env_yaml = '/tmp/mesos_env.yml'
}

function install-salt() {

  mkdir -p /var/cache/salt-install
  cd /var/cache/salt-install

  cat > /usr/sbin/policy-rc.d <<EOF
#!/bin/sh
echo "Salt shall not start." >&2
exit 101
EOF
  chmod 0755 /usr/sbin/policy-rc.d

  echo "deb http://debian.saltstack.com/debian wheezy-saltstack main" >> /etc/apt/sources.list.d/salt.list
  wget -q -O- "http://debian.saltstack.com/debian-salt-team-joehealy.gpg.key" | apt-key add -
  echo "== Refreshing package update =="
  until apt-get update; do 
    "== apt-get update failed, retrying =="
    echo sleep 5
  done

  apt-get install salt-master -y
  rm /usr/sbin/policy-rc.d
  
  # Log a timestamp
  echo "== Finished installing Salt =="
}

function disable-salt-minion() {
  if service salt-minion status >/dev/null; then
    echo "salt-minion started in defiance of runlevel policy, aborting startup." >&2
    service salt-minion stop
  fi
  echo manual > /etc/init/salt-minion.override
  update-rc.d salt-minion disable
}

function configure-salt() {
  fix-apt-sources
  mkdir -p /etc/salt/minion.d
  setup-masterless-salt
  setup-slave-role
  install-salt
  disable-salt-minion
}

function run-salt() {
  salt-call --local state.highstate || true
}

echo "==== Deploying mesos ===="
generate-broken-motd
install-salt
run-salt
generate-fixed-motd
