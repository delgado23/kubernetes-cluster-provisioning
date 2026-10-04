# Kubernetes Cluster Provisioning

> **This was written entirely with Claude just to mess around building Kubernetes clusters in my homelab. Sharing it in case it's useful to anyone.**

Ansible automation for provisioning and managing a highly-available Kubernetes cluster on Proxmox via Foreman. Targets AlmaLinux 10, uses Flannel CNI, MetalLB, Traefik, cert-manager (Cloudflare DNS-01), Longhorn storage, Prometheus, Headlamp, ArgoCD, and Descheduler.

## Cluster Architecture

| Component | Details |
|---|---|
| Node OS | AlmaLinux 10, either package-mode (dnf) or image-mode (bootc/ostree) — auto-detected per node, see [OS Image Mode](#os-image-mode-bootc) |
| Control plane | 1–3 nodes (primary always included, up to 2 secondaries), HA via HAProxy + keepalived |
| Workers | 1–99 nodes, Longhorn LVM on dedicated data disk |
| Networking | Flannel VXLAN, MetalLB L2 LoadBalancer |
| Ingress | Traefik v3 with HTTP→HTTPS redirect |
| TLS | cert-manager + Let's Encrypt + Cloudflare DNS-01, wildcard cert for `*.ingress_domain` — automatically mirrored to all addon namespaces by reflector and renewed without manual intervention |
| Storage | Longhorn with XFS LVM partition |
| GitOps | ArgoCD |
| SSO | Authentik (external) — ForwardAuth for Traefik/Longhorn, native OIDC for ArgoCD/Headlamp |
| Monitoring | Prometheus + AlertManager (kube-prometheus-stack), Longhorn-backed PVCs, node-exporter + kube-state-metrics; Grafana replaced by Headlamp plugin |
| Dashboard | Headlamp (Kubernetes UI) with Prometheus metrics plugin for in-UI charts |
| Tooling | kubectl, Helm, k9s on all control plane nodes |
| etcd encryption | XSalsa20-Poly1305 (secretbox) authenticated encryption at rest for all Secrets, configured at kubeadm init time |
| Pod Security Standards | Namespace-scoped enforcement — `privileged` for storage/network system namespaces, `baseline`/`restricted` for application namespaces |
| Policy engine | Kyverno in Enforce mode — blocks pods violating latest image tags, missing resource limits, privileged containers, and host namespace use |
| NetworkPolicies | Default-deny ingress in all addon namespaces; kube-system (CoreDNS allow), kube-flannel (VXLAN allow), and monitoring (egress scoped to DNS, API server, scrape targets) |
| Pod rebalancing | Descheduler CronJob (every 6 h) — evicts pods from hot nodes, spreads Deployment replicas, enforces topology spread constraints |
| Addon versions | Resolved from GitHub releases at runtime — always installs latest |
| VM hotplug | CPU and memory hotplug enabled at provision time; `cores` and `vcpus` set to the per-role max (6 CP / 4 worker) so the autoscaler adjusts `vcpus` live without reboots |

Node counts are defined in `vars/vms.yml` and filtered at runtime via `controlplane_node_count` and `worker_node_count` (see [AWX Survey Variables](#awx-survey-variables)).

## Prerequisites

- Ansible control node with collections:
  - `ansible.posix`
  - `community.general`
  - `theforeman.foreman`
  - `freeipa.ansible_freeipa`
- Foreman with a Proxmox compute resource configured
- Ansible Vault password for `vars/vault.yml`
- Cloudflare API token with `Zone/DNS/Edit` permission on your domain

## Repository Structure

```
.
├── main.yml              # Full cluster provisioning pipeline
├── autoscale.yml                  # Vertical autoscaler — scales CPU/RAM up and down via Proxmox based on utilisation
├── autoscale-horizontal.yml       # Horizontal autoscaler — adds or removes worker nodes via AWX based on average utilisation
├── maintenance.yml       # Rolling OS maintenance (drain/update/reboot/uncordon)
├── upgrade.yml           # Rolling Kubernetes minor upgrade (image-mode, kubeadm + bootc switch)
├── upgrade-rollback.yml  # Roll named nodes back to their previous bootc image
├── upgrade-dr-restore.yml # After `kubeadm upgrade apply`: restore the pre-upgrade etcd snapshot
├── wipe.yml              # Remove all nodes from Foreman, FreeIPA, and Zabbix
├── mirror-github-repos.yml # Pull-mirror upstream GitHub deps into local Gitea
├── handlers/
│   └── main.yml          # Shared handlers (e.g. Reload ssh)
├── templates/
│   ├── login_duo.conf.j2 # DUO Unix SSH integration config
│   └── 00-duo.conf.j2    # sshd_config.d snippet enabling ForceCommand login_duo
├── vars/
│   ├── foreman.yml       # Foreman connection settings and compute profile IDs
│   ├── freeipa.yml       # FreeIPA ipaclient vars (sensitive values sourced from vault)
│   ├── vault.yml         # Ansible Vault: API tokens and passwords
│   ├── vms.yml           # VM definitions (names, hostgroups, Foreman parameters)
│   ├── kubernetes.yml    # Image sources (Katello core-image mirror, node images) + cluster node lists
│   ├── upgrade.yml       # Play settings for upgrade.yml / upgrade-rollback.yml
│   ├── vm_defaults.yml   # Default VM hardware specs
│   └── gitea.yml         # Local Gitea mirror bases (version/raw/package fetch)
└── roles/
    ├── gitea_mirrors/        # Create/refresh GitHub pull-mirrors + republish release assets in Gitea
    ├── vm_provisioning/       # Create VMs in Foreman/Proxmox, filters by node counts
    ├── proxmox_postconfig/    # EFI disk, rename, tags, CPU/memory hotplug via Proxmox API
    ├── enterprise_linux/      # Base OS provisioning: repos, packages, atop, DUO install
    ├── common/                # K8s prerequisites: containerd, kubelet, firewall
    ├── controlplane_infra/    # HAProxy, keepalived, controlplane firewall rules
    ├── worker_storage/        # LVM + XFS setup for Longhorn on worker data disk, iSCSI
    ├── cluster_bootstrap/     # kubeadm init/join, Flannel, kubelet-serving-cert-approver, tooling
    ├── worker_join/           # Join worker nodes to the cluster
    ├── cluster_addons/        # MetalLB, Traefik, Longhorn, reflector, cert-manager, Prometheus, Headlamp, ArgoCD, Descheduler
    ├── node_maintenance/      # Drain, update, reboot, uncordon (drain/wait_ready/uncordon reused by k8s_upgrade)
    ├── k8s_upgrade/           # Preflight, per-node upgrade, etcd snapshot, gates, postflight
    └── cluster_cleanup/       # Remove hosts from Foreman and FreeIPA
```

## Idempotency and Scale-Out

The playbook is safe to re-run against a partially or fully provisioned cluster:

- **Linux provisioning** — base OS hardening (`enterprise_linux` role), FreeIPA enrollment (`ipaclient`), DUO SSH 2FA config, and reboot are guarded by a sentinel file at `/root/.provisioned`. Nodes that have already been through this phase are skipped on re-runs.
- **Prep phase** — each node checks for `/etc/kubernetes/kubelet.conf` before running `common`, `controlplane_infra`, and `worker_storage`. Nodes already in the cluster skip those roles entirely.
- **Bootstrap phase** — `join_secondary` generates fresh join credentials at runtime via `delegate_to` against an existing control plane node (no cross-play hostvars required). It checks for `kubelet.conf` and skips the join if the node is already a cluster member. When `controlplane_node_count=0`, the entire bootstrap phase is skipped.
- **Worker join** — workers are skipped if they already have `kubelet.conf`. The join token is generated fresh via `delegate_to` directly against a control plane node, so running with `--limit` scoped to only new workers works without needing the control plane in scope.
- **Cluster add-ons** — skipped when `controlplane_node_count=0` (already installed on the running cluster).

### AWX Compatibility

All plays target static Foreman inventory groups. Primary vs secondary control plane targeting is done via `foreman_params.k8s_role` (an inventory variable set by the Foreman plugin) rather than dynamic groups — `group_by` and `add_host` do not reliably persist across plays in AWX's isolated executor.

## OS Image Mode (bootc)

Nodes can run on traditional **package-mode** AlmaLinux 10 (mutable filesystem, packages installed at runtime via dnf) or on **image-mode** AlmaLinux built with [bootc](https://containers.github.io/bootc/) (an immutable, ostree-based OS shipped as a container image). The playbooks support both transparently — no survey variable is involved.

Each node detects its own mode early in the prep phase by checking for `/run/ostree-booted`, and sets an `image_mode` fact that gates the rest of the run. On image-mode hosts:

- **Packages are baked into the image, not installed at runtime.** `/usr` is immutable and the dnf module fails (`pkg_mgr` is detected as `atomic_container`), so all `dnf`/package-install tasks are skipped. The required software — containerd, kubelet, kubeadm, ipa-client, etcd user/group, python3-pyyaml, etc. — is expected to be present in the bootc image already (the homelab builds `kubernetes-common`, `kubernetes-controlplane`, and `kubernetes-worker` images for this).
- **Config and unit management still run.** Tasks that only write under `/etc` or toggle systemd units work unchanged — those paths stay writable on an ostree system.
- **Tooling installs to `/usr/local/bin`.** Helm and k9s are installed from upstream release tarballs into `/usr/local/bin` (a writable symlink to `/var/usrlocal` in the bootc image) rather than via dnf, so they work identically on both modes.
- **OS updates use `bootc upgrade`, not dnf.** See [Node Maintenance](#node-maintenance-rolling-os-updates) below.
- **The image is the Kubernetes version pin.** foreman-bootc tags the node images with the Kubernetes version (`kubernetes-controlplane:1.35.5`), and `kubeadm-config.yml.j2` renders `kubernetesVersion` from the node's own `kubeadm version`, so a new cluster runs exactly the image's version. (Unpinned, `kubeadm init` asked dl.k8s.io for the newest patch: a 1.35.5 image ran a v1.35.9 control plane.) Moving to a new minor is [Kubernetes Upgrade](#kubernetes-upgrade-rolling), not maintenance.
- **Core images come from Foreman.** `imageRepository` and containerd's sandbox (pause) image point at the Katello mirror of registry.k8s.io (`vars/kubernetes.yml`), maintained by foreman-content-views.

Package-mode nodes are unaffected — `image_mode` is `false` and every dnf/package task runs as before. A cluster can even mix the two modes; detection is per-node.

## Upstream Dependency Mirrors (Gitea)

The build never reads from `api.github.com` / `github.com` directly. GitHub's
unauthenticated release API throttles to 60 req/hr and serves transient `504`s
that abort whole builds. Instead, every upstream dependency is **pull-mirrored
into the local Gitea** (`git.garaventaville.com`, org `github-mirror`) and the
build reads from there (anonymously — no token on the nodes).

Run once to set up; re-runnable any time and self-refreshing (each mirror
re-pulls from upstream on a 24h interval):

```bash
GITEA_MIRROR_TOKEN=... ansible-playbook mirror-github-repos.yml   # or vault_gitea_token
```

How each dependency is served (see `roles/gitea_mirrors`, `vars/gitea.yml`):

- **Versions** — resolved from the mirror's **git tags** (`/api/v1/repos/github-mirror/<repo>/tags`), picking the highest stable semver (`community.general.version_sort`). Gitea reliably mirrors tags; it does **not** import GitHub *releases*, so tags (not the releases API) are the source of truth.
- **Manifests in the repo tree** (metallb, cert-approver, flannel `Documentation/kube-flannel.yml`) — served from `…/github-mirror/<repo>/raw/tag/<tag>/<path>`.
- **Release-only assets** (k9s binary + checksums, metrics-server `components.yaml`) — Gitea doesn't mirror release assets, so `mirror-github-repos.yml` downloads them from GitHub server-side and republishes to Gitea's **generic package registry** (`/api/packages/github-mirror/generic/<repo>/<version>/<file>`), which the build then reads.

Add a new mirrored dependency by appending to `gitea_mirror_repos` (and, if it's
a release-only asset, `gitea_mirror_assets`) in
`roles/gitea_mirrors/defaults/main.yml` and re-running the playbook.

## Vault Variables

Encrypt `vars/vault.yml` with `ansible-vault encrypt vars/vault.yml`. Required keys:

| Variable | Description |
|---|---|
| `vault_proxmox_api_token` | Proxmox API token secret |
| `vault_proxmox_host` | Proxmox host and port (e.g. `pve-01.example.com:8006`) |
| `vault_proxmox_user` | Proxmox user for API auth (e.g. `user@Authentik`) |
| `vault_cloudflare_api_token` | Cloudflare API token for DNS-01 |
| `vault_cloudflare_email` | Cloudflare account email |
| `vault_domain` | Base domain (e.g. `example.com`) — drives `k8s_api_endpoint`, `ingress_domain`, `foreman_domain`, `wildcard_secret_name`, and FreeIPA realm |
| `vault_authentik_url` | Authentik base URL (e.g. `https://auth.example.com`) |
| `vault_foreman_url` | Foreman base URL (e.g. `https://foreman.example.com`) |
| `vault_etcd_encryption_key` | Base64-encoded 32-byte key for etcd secretbox (XSalsa20-Poly1305) encryption at rest |
| `vault_duo_ikey` | DUO Unix integration key (application identifier) |
| `vault_duo_secret_key` | DUO Unix integration secret key for SSH 2FA |
| `vault_duo_host` | DUO API hostname (e.g. `api-xxxxxxxx.duosecurity.com`) |
| `vault_authentik_argocd_client_id` | OIDC client ID for the ArgoCD application in Authentik |
| `vault_authentik_argocd_client_secret` | OIDC client secret for the ArgoCD application in Authentik |
| `vault_authentik_headlamp_client_id` | OIDC client ID for the Headlamp application in Authentik |
| `vault_authentik_headlamp_client_secret` | OIDC client secret for the Headlamp application in Authentik |
| `vault_foreman_user` | Foreman username |
| `vault_foreman_password` | Foreman password |
| `vault_ipaadmin_password` | FreeIPA admin password |
| `vault_ipa_server` | FreeIPA server hostname (e.g. `ipa.example.com`) |
| `vault_ipadomain` | FreeIPA domain (e.g. `example.com`) |
| `vault_ntp_servers` | List of NTP server FQDNs for FreeIPA client enrollment |
| `vault_keepalived_password` | Keepalived VRRP authentication password |
| `vault_awx_url` | AWX/Ascender base URL (e.g. `https://ascender.example.com`) |
| `vault_awx_token` | AWX API token — used to enable/disable autoscale schedules after build/wipe |
| `vault_gitea_token` | (optional) Gitea token used **only** by `mirror-github-repos.yml` to create and refresh the upstream mirrors (the cluster build reads the mirrors anonymously). Usually unset: the token normally comes from `GITEA_MIRROR_TOKEN`, injected by the Ascender "Gitea mirror bot" credential for Gitea user `svc-gitea-mirror`. That user is in the `github-mirror` org's `mirror-automation` team (write on code/releases/packages, can create org repos, not owner), and its token has scopes `write:repository`, `write:package` and `read:organization`. The token is also stored in OpenBao at `secret/automation/gitea-mirror-bot`. |
| `vault_gitea_url` | (optional) Gitea base URL; defaults to `https://git.garaventaville.com` |
| `vault_github_token` | (optional) GitHub PAT handed to Gitea so its own upstream sync is not rate-limited; empty is fine for public repos |
| `vault_zabbix_url` | (optional) Zabbix base URL; defaults to `https://zabbix.garaventaville.com` |
| `vault_zabbix_api_token` | Zabbix API token used to deregister a node's host entry on wipe/scale-in (`host.get`/`host.delete` only) — use a dedicated user/role scoped to just those two methods and read-write on the `Linux servers` host group, not a Super Admin token |

Generate the secretbox key once and store it in the vault — **do not change it after the cluster is provisioned** (doing so requires a full secret re-encryption rotation):

```bash
head -c 32 /dev/urandom | base64
```

## AWX Survey Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `wipe_cluster` | Boolean | `false` | When `true`, removes all existing cluster nodes from Foreman, FreeIPA, and Zabbix first, then provisions a fresh cluster using the current `controlplane_node_count` and `worker_node_count` values. |
| `controlplane_node_count` | Integer | 1 | Number of control plane nodes to provision this run. Set to `0` when only adding worker nodes — this skips the bootstrap and cluster add-ons phases entirely. Max 3. |
| `worker_node_count` | Integer | 1 | Number of worker nodes to add this run. The provisioning role queries Foreman for existing workers with the configured prefix and starts numbering from the next available index. Running with `worker_node_count=3` twice produces 6 workers total. |
| `cluster_env` | String | `prod` | `prod` or `test`. Controls node naming prefixes, API endpoint hostname, ingress domain, FreeIPA wildcard DNS record, and ACME endpoint. `prod` → `k8s-cp` / `k8s-worker`, `k8s-api.<domain>`, `k8s.<domain>`, Let's Encrypt production. `test` → `k8s-test-cp` / `k8s-test-worker`, `k8s-test-api.<domain>`, `k8s-test.<domain>`, Let's Encrypt staging. Both clusters can run side-by-side — they use separate DNS records and name prefixes. |
| `letsencrypt_staging` | Boolean | derived | Auto-derived from `cluster_env` (`true` when `test`, `false` when `prod`). When `true`, uses the Let's Encrypt **staging** ACME endpoint — certs are not browser-trusted but have no rate limits. Only set this directly if you need to decouple it from `cluster_env`. |
| `k8s_api_endpoint` | String | derived | Auto-derived from `cluster_env` — `k8s-api.<vault_domain>` for prod, `k8s-test-api.<vault_domain>` for test. An A record pointing to `k8s_api_endpoint_ip` is created in FreeIPA during provision and removed on wipe. |
| `k8s_api_endpoint_ip` | String | derived | Keepalived VIP, derived from `cluster_env`: prod `172.16.0.30`, test `172.16.0.29`. Also used as `keepalived_vip`. Don't override it unless you have checked that the new IP is free. A brand-new cluster refuses to provision on a VIP that already answers on :6443. |
| `metallb_pool` | String | derived | MetalLB L2 pool, derived from `cluster_env`: prod `172.16.0.61-172.16.0.71`, test `172.16.0.50-172.16.0.60`. Traefik claims the first free IP from it. Must never overlap the other cluster's pool. |
| `k8s_pod_cidr` | String | `10.244.0.0/16` | Pod network CIDR passed to kubeadm and used in firewall rules. Must not overlap with your node or service networks. Change only if the default conflicts with your infrastructure. |
| `k8s_service_cidr` | String | `10.96.0.0/12` | Kubernetes service network CIDR passed to kubeadm and used in firewall rules and NetworkPolicies. Must not overlap with your node or pod networks. Change only if the default conflicts with your infrastructure. |

`wipe_cluster=true` runs a full rebuild: existing nodes are removed from Foreman, FreeIPA, and Zabbix, then provisioning continues immediately with the rest of the survey values (`controlplane_node_count`, `worker_node_count`). The Foreman queries in the provisioning role will see zero existing nodes after the wipe, so numbering restarts from `01`.

Neither control plane nor worker nodes have hardcoded names. Both are generated at runtime from a prefix and a counter. The prefix is derived from `cluster_env`: `prod` produces `k8s-cp` / `k8s-worker`; `test` produces `k8s-test-cp` / `k8s-test-worker`. Foreman hostgroup strings are configured in `vars/vms.yml`.

Prod and test clusters can run side-by-side. Each has its own FreeIPA DNS wildcard (`*.k8s` vs `*.k8s-test`), so wiping one doesn't affect the other's ingress. Their API VIP, MetalLB pool and keepalived `virtual_router_id` (prod 51, test 52) all share one L2 network, so `vars/vms.yml` derives all three from `cluster_env`. They are **not** survey questions; overriding them with extra vars is only for a deliberate, checked change. (On 2026-10-04 a survey default put a test build on prod's live VIP, and the shared VRRP id let it contend for that VIP.)

## Usage

### Full Cluster Provisioning

Run the entire pipeline end to end:

```bash
ansible-playbook main.yml --ask-vault-pass
```

Add 3 control plane nodes and 5 worker nodes:

```bash
ansible-playbook main.yml --ask-vault-pass -e "controlplane_node_count=3 worker_node_count=5"
```

### Removing Worker Nodes (Scale Down)

Drains the node, removes it from Kubernetes, resets kubeadm state on the VM itself, then deletes the host from Foreman, FreeIPA, and Zabbix. Pass one or more short hostnames (without the domain suffix):

```bash
# Remove a single worker
ansible-playbook remove-workers.yml --ask-vault-pass -e "remove_workers=k8s-worker-03"

# Remove multiple workers
ansible-playbook remove-workers.yml --ask-vault-pass -e "remove_workers=k8s-worker-03,k8s-worker-04"
```

In AWX, configure `remove_workers` as a **Text** survey question. The playbook validates that each named host exists in Foreman before proceeding.

The playbook also clears the `/root/.provisioned` sentinel on each removed node, so the same VM can be re-provisioned cleanly if it is added back later.

### Adding Worker Nodes to an Existing Cluster

Set `controlplane_node_count=0` to skip bootstrap and cluster add-ons. The playbook provisions the new VMs, preps them, joins them to the cluster, and stops — existing nodes that already have `kubelet.conf` are skipped during prep:

```bash
ansible-playbook main.yml --ask-vault-pass -e "controlplane_node_count=0 worker_node_count=2"
```

### Vertical Autoscaling

`autoscale.yml` queries the metrics-server for current CPU and memory utilisation on each node and adjusts resources via the Proxmox API — scaling up when utilisation is high, and reclaiming when it is low. The VM does not reboot — CPU changes are delivered live by adjusting `vcpus` (Proxmox hotplug), and memory changes are delivered live via memory hotplug. Both are enabled automatically during the provision phase, which also sets `cores` to the per-role maximum so the full range is available for hotplug from first boot.

| Role | CPU min | CPU max | RAM min | RAM max |
|---|---|---|---|---|
| Control plane | 2 cores | 6 cores | 2 GB | 6 GB |
| Worker | 2 cores | 4 cores | 2 GB | 4 GB |

Run manually:

```bash
ansible-playbook autoscale.yml --ask-vault-pass -e "cluster_env=prod"
```

Designed to run on an AWX schedule (e.g. every 30 minutes). Survey variables:

| Variable | Default | Description |
|---|---|---|
| `cluster_env` | `prod` | Target cluster — `prod` or `test` |
| `cpu_scale_threshold` | `70` | Add a core when any node's CPU% exceeds this |
| `memory_scale_threshold` | `80` | Add 1 GB when any node's memory% exceeds this |
| `cpu_scaledown_threshold` | `30` | Remove a core when any node's CPU% drops below this |
| `memory_scaledown_threshold` | `40` | Remove 1 GB when any node's memory% drops below this |

Each run adds or removes at most 1 core or 1 GB per node, clamped to the per-role min/max. If all nodes are within their thresholds the playbook reports "no scaling needed" and exits cleanly. Requires `metrics-server` running in the cluster (installed automatically).

### Horizontal Autoscaling

`autoscale-horizontal.yml` monitors average worker utilisation and provisions or removes entire worker nodes by triggering the existing AWX job templates. Designed to run every 15 minutes.

**Scale-out** — when average worker CPU or memory exceeds the threshold, launches "Scale Out Worker Nodes" (template 54) to add one worker via the full Foreman provisioning pipeline (~20–30 min).

**Scale-in** — when average worker CPU and memory both drop below the threshold and removing a node won't push remaining workers over the scale-out threshold, launches "Scale Down Worker Nodes" (template 53) against the least-loaded worker (drain → kubeadm reset → Foreman/FreeIPA removal).

**Cooldown** — skips all scaling if a scale-out or scale-in job is already running or pending, preventing concurrent operations.

| Variable | Default | Description |
|---|---|---|
| `cluster_env` | `prod` | Target cluster — `prod` or `test` |
| `worker_scale_out_cpu_threshold` | `70` | Add a worker when avg CPU% exceeds this |
| `worker_scale_out_mem_threshold` | `80` | Add a worker when avg memory% exceeds this |
| `worker_scale_in_cpu_threshold` | `20` | Remove a worker when avg CPU% drops below this |
| `worker_scale_in_mem_threshold` | `30` | Remove a worker when avg memory% drops below this |
| `worker_min_count` | `2` | Never scale below this many workers |
| `worker_max_count` | `10` | Never scale above this many workers |

Both vertical and horizontal autoscale schedules are enabled automatically when a cluster build succeeds and disabled when a cluster is wiped.

### Run a Specific Phase

Each phase is tagged. You can run or skip individual phases:

```bash
# Phase 1: Create VMs in Proxmox via Foreman
ansible-playbook main.yml --tags provision

# Phase 2: Install K8s prerequisites on all nodes (skips already-provisioned nodes)
ansible-playbook main.yml --tags prep

# Phase 3: Bootstrap control plane (kubeadm init, join secondaries, cert-approver, tooling)
ansible-playbook main.yml --tags bootstrap

# Phase 4: Join workers to the cluster and label them
ansible-playbook main.yml --tags workers

# Phase 5: Install add-ons (MetalLB, Traefik, Longhorn, reflector, cert-manager, Prometheus, Headlamp, ArgoCD, Descheduler)
ansible-playbook main.yml --tags addons

# Skip VM provisioning for re-runs against existing nodes
ansible-playbook main.yml --skip-tags provision

# Ingress diagnostics (dump Traefik/cert state)
ansible-playbook main.yml --tags debug
```

### Renew Control Plane Certificates

kubeadm certificates expire after 1 year. Run this before expiry or whenever `kubeadm certs check-expiration` shows certs approaching their deadline. Processes one control plane at a time so the cluster stays available throughout.

```bash
ansible-playbook renew-certs.yml

# Single node only
ansible-playbook renew-certs.yml --limit k8s-cp-01.example.com
```

Covers all kubeadm-managed certs (apiserver, etcd, front-proxy, admin/controller-manager/scheduler kubeconfigs). Does **not** touch CA certs (10-year lifetime), kubelet client certs (auto-rotated by kubelet), or kubelet serving certs (handled by kubelet-serving-cert-approver).

### Node Maintenance (Rolling OS Updates)

Drains, updates the OS, reboots, and uncordons nodes. Control planes are rolled one at a time; workers two at a time. The maintenance playbook runs standalone (it never runs `main.yml`'s prep play), so it re-detects `image_mode` from `/run/ostree-booted` in its own `pre_tasks`.

The OS update mechanism is mode-aware (see [OS Image Mode](#os-image-mode-bootc)):

- **Package-mode** nodes are patched in place with `dnf upgrade "*"` and always reboot (kernel and other in-place patches require it).
- **Image-mode** nodes run `bootc upgrade`, which stages the newest build of the image the node already tracks (rebuilt with patches by the foreman-bootc pipeline) for the next boot. The reboot is **conditional** — the node only reboots when bootc actually staged a new image; rebooting into an identical deployment is skipped.

```bash
ansible-playbook maintenance.yml --ask-vault-pass

# Workers only
ansible-playbook maintenance.yml --limit foreman_almalinux10_kubernetesworkernode

# Override drain timeout
ansible-playbook maintenance.yml -e "drain_timeout=600"
```

### Kubernetes Upgrade (Rolling)

`upgrade.yml` moves one image-mode cluster **one minor up** (kubeadm can't skip
minors, so 1.35 -> 1.37 is two runs), or to a later patch of its minor, a node
at a time, with no rebuild and no data movement.

```bash
# cluster_env has no default here -- the playbook never assumes prod
ansible-playbook upgrade.yml -e cluster_env=test -e k8s_target_version=1.36.5 --ask-vault-pass
```

**Before a run:**

1. foreman-content-views (JT 33) carries the target minor: the `AlmaLinux 10 - Kubernetes 1.NN` RPM repo and the `Kubernetes Images` core-image mirror.
2. foreman-bootc has built and promoted `kubernetes-{controlplane,worker}:<target>`. Its Containerfiles pin the kube* NVRs.
3. Disable this cluster's autoscale schedules and the maintenance job (JT 49), and silence node alerts.
4. Don't run the bootc image build (Ascender JT 62 / "OS Update Workflow") during the upgrade. It re-pushes the tag, and the run refuses any image whose digest differs from the one preflight resolved.
5. Check that the deployed add-ons support the target minor.

**What it does** (`roles/k8s_upgrade`):

| Phase | |
|---|---|
| Preflight (read-only) | Checks the inputs:<br>- the target is one minor up (or a later patch)<br>- every node is Ready, schedulable and no newer than the target<br>- no Pending pods; every Deployment/StatefulSet/DaemonSet is fully available (this covers the Kyverno and cert-manager webhooks)<br>- all etcd members are healthy; certs aren't expiring within 7 days; `/sysroot` has room<br>- a server-side drain dry run passes on every node (Longhorn `instance-manager` PDBs are expected to block it until a node is really cordoned)<br>- the node-image digests resolve<br>- the target kube-apiserver is in the mirror<br><br>It also pulls the *current* version's core images through Katello, which caches them for a rollback. |
| Control planes, one at a time | 1. `bootc switch` stages the target image, with its digest checked.<br>2. Pre-pull the target core images.<br>3. **First CP only:** take an etcd snapshot (verified, sha256'd, copied to every CP).<br>4. Drain.<br>5. **First CP only:** a one-time switch of `imageRepository` to the Katello mirror.<br>6. Run `kubeadm upgrade apply` (first CP) or `upgrade node` (the others), **using kubeadm from the staged deployment**, with `--patches /etc/kubernetes/kubeadm-patches`.<br>7. Render the containerd config, then reboot into the new image (new kubelet).<br>8. Wait for Ready, the static pods and etcd health, then uncordon. |
| Gate | Runs after the first CP:<br>- `/readyz` through the VIP<br>- all nodes Ready, all workloads available, Argo CD apps Healthy<br>- `k8s_upgrade_smoke_urls` answering |
| Workers, one at a time | Wait until every Longhorn volume is healthy, then stage, pre-pull, drain, `kubeadm upgrade node`, reboot, Ready, uncordon. The same gate runs after the first worker. |
| Settle (after **every** node) | Pause `k8s_upgrade_settle_seconds` (60s) so evicted pods can reschedule. Then wait, up to `k8s_upgrade_settle_retries` × 15s (10 min), for no Pending pods, every workload fully available and every Longhorn volume healthy. Only then is the next node drained. A node that never settles stops the roll. |
| Postflight | Longhorn volumes are healthy again (the last node's replicas have rebuilt), every node is on the target digest, the kubeadm config is at the target with the mirror `imageRepository`, every control-plane/kube-proxy/CoreDNS image comes from the mirror, etcd is healthy, and the final gate passes. |

kubeadm upgrades the node's control plane and kubelet config **before** the node
boots the new kubelet, so the kubelet is never newer than the API server.

- **Failure handling:** any failure before a node's reboot discards the staged
  image (`rpm-ostree cleanup --pending`), so the node keeps booting its current
  image. The node is left cordoned and the run stops; later nodes are untouched.
- **Re-runs are safe:**
  - nodes already on the target digest are only uncordoned
  - `upgrade apply` runs only while the cluster's kubeadm config is still on the old version
- **Backups:** every node keeps backups in `/var/lib/k8s-upgrade-backups`
  (root-only). `/var` survives both the switch and a rollback, and nightly
  vzdump → PBS takes the backups off-cluster. They are:
  - `node-state-<ts>-v<old>.tgz`: `/etc/kubernetes`, the kubelet config and the containerd config
  - on every control plane, the etcd snapshot

  They include the cluster CA and the Secret encryption config.

After the run, point the host groups at the new tag (foreman-bootc
`setup_kubernetes_image_mode.rb` with `K8S_TAG=<target>`), so scale-out builds
new nodes on it, and re-enable the schedules.

#### Rolling back

Which rollback applies depends on how far the run got:

| Point | Rollback |
|---|---|
| A worker, any time during a hop | `upgrade-rollback.yml` with `k8s_rollback_nodes=<fqdn>`. It drains the node, restores the kubelet config from its node-state backup, runs `bootc rollback` (or `bootc switch` to `-e k8s_rollback_image=<repo>:<tag>` if JT 49 has since replaced the rollback slot), reboots and uncordons. It refuses to go more than one minor back, or to break the kubelet/API-server version skew. |
| A control plane, before the first `kubeadm upgrade apply` | The same playbook. Nothing has changed in the cluster yet. |
| A control plane, after the first `kubeadm upgrade apply` | This is the **point of no return**. Rolling back the image only brings back the old kubelet: the new control-plane static pods and the cluster state stay, and the 1.37 hop also moves etcd 3.6 → 3.7. The only way back is restoring the pre-apply etcd snapshot on **all** members. |

For that last case, run **`upgrade-dr-restore.yml`**. It is destructive: every
cluster write after the snapshot is lost, and the API is down while it runs.

```bash
ansible-playbook upgrade-dr-restore.yml -e cluster_env=test -e k8s_dr_confirm=restore-test
#   -e k8s_dr_snapshot=<file>   (default: the newest etcd-<env>-*.db on the primary CP)
#   -e k8s_dr_image=<repo>:<old tag>  (if a CP's rollback slot no longer holds the old version)
```

It encodes the procedure rehearsed on the test cluster on 2026-10-04 (1.36.5 → 1.35.9):

1. Pause autoscaling. During the rehearsal the horizontal autoscaler added a worker mid-restore.
2. Check that every CP holds the same snapshot (sha256), its pre-upgrade node-state backup, and a bootc deployment of the old version.
3. Stop the control plane on **all** CPs by moving their manifests aside.
4. On every member, restore the snapshot with that member's own identity:
   - `etcdutl` from the **old** etcd image in the mirror
   - a new cluster token
   - `--bump-revision --mark-compacted`, so controllers and kubelets re-list
   - the data dir owned like the original (`etcd:etcd 0700` on these images)
5. Stage the old manifests from the **node-state backup** in `/var` (images repointed at the mirror), restore the old kubelet config, and point its `staticPodPath` at the staged manifests. kubeadm's own `/etc/kubernetes/tmp/kubeadm-backup-*` can't be used: kubeadm deletes it after a successful upgrade.
6. `bootc rollback` and reboot all CPs.
7. Once etcd has quorum, one CP at a time: copy the old manifests into the booted deployment's `/etc` and point `staticPodPath` back.
8. Roll back every worker now ahead of the control plane (`upgrade-rollback.yml` with `k8s_rollback_nodes=auto`), check that everything is on the old version, let the cluster settle, and resume autoscaling.

**Why the `staticPodPath` detour:** on bootc, `/etc` belongs to each deployment,
but `/var` is shared. `kubeadm upgrade` writes the *new* manifests while the node
still runs the old deployment, so `bootc rollback` boots an `/etc` that still
holds the new control plane. It would start that on the restored data, and for
1.37 that means etcd 3.7 on 3.6 data, which can't be undone. The kubelet reads
its config from `/var`, so pointing it at manifests staged in `/var` means it
only ever starts the old control plane, whatever the booted `/etc` holds.

Everything written to the cluster after the snapshot is lost (Argo CD
re-syncs what's in Git).

### Wipe Cluster

Queries Foreman for all hosts matching the CP and worker name prefixes for the selected `cluster_env` and removes them from Foreman and FreeIPA, along with the keepalived VIP and wildcard DNS records for that environment. Finds everything regardless of how many scale-out runs were done. Does not touch the Proxmox VMs themselves.

```bash
# Wipe the prod cluster
ansible-playbook wipe.yml --ask-vault-pass

# Wipe the test cluster (leaves prod untouched)
ansible-playbook wipe.yml --ask-vault-pass -e "cluster_env=test"
```

## VM Definitions

### Proxmox Node Placement

Each VM is created on a node from `proxmox_placement_nodes` in `vars/foreman.yml`, round-robin by the number in its name: `k8s-cp-01` goes on the first node, `k8s-cp-02` on the second, `k8s-worker-04` wraps back to the first. The compute profiles pin a single `node_id`, so the node is sent per host at create time to override it. Without this, every VM landed on one host, and losing that host took etcd quorum with it. List only nodes that hold LINSTOR replicas of the VM disks.

`proxmox_postconfig` and `autoscale.yml` look up each VM's current node through `/cluster/resources`, so VMs can also be live-migrated after the build without breaking either one.

Edit `vars/vms.yml` to configure nodes. There are no hardcoded hostnames — both control plane and worker names are generated at runtime from a prefix and a counter. The prefix is derived from the `cluster_env` survey var:

- `cluster_env=prod` → `k8s-cp-01`, `k8s-worker-01`, …
- `cluster_env=test` → `k8s-test-cp-01`, `k8s-test-worker-01`, …

Control plane configs are ordered: index 0 is always the primary. Only the first `controlplane_node_count` entries are provisioned.

```yaml
# Hostgroups — must match your Foreman setup
controlplane_hostgroup: "AlmaLinux 10/Kubernetes Controlplane Node"
worker_hostgroup:       "AlmaLinux 10/Kubernetes Worker Node"

# Name prefixes are derived from cluster_env (set in AWX survey):
#   prod → k8s-cp / k8s-worker
#   test → k8s-test-cp / k8s-test-worker

controlplane_configs:
  # Index 0 — primary, always provisioned
  # k8s_api_endpoint, k8s_api_endpoint_ip and metallb_pool are derived from
  # cluster_env in vars/vms.yml (never survey questions); k8s_pod_cidr and
  # k8s_service_cidr are AWX survey vars injected at runtime.
  - host_parameters:
      - { name: k8s_role,            value: primary }
      - { name: keepalived_state,    value: MASTER }
      - { name: keepalived_priority, value: 101 }
      - { name: cluster_name,        value: my-cluster }
      - { name: k8s_api_endpoint,    value: "{{ k8s_api_endpoint }}" }
      - { name: k8s_api_endpoint_ip, value: "{{ k8s_api_endpoint_ip }}" }
      - { name: metallb_pool,        value: "{{ metallb_pool }}" }

  # Index 1 — secondary, provisioned when controlplane_node_count >= 2
  - host_parameters:
      - { name: k8s_role,            value: secondary }
      - { name: keepalived_state,    value: BACKUP }
      - { name: keepalived_priority, value: 100 }

  # Index 2 — secondary, provisioned when controlplane_node_count >= 3
  - host_parameters:
      - { name: k8s_role,            value: secondary }
      - { name: keepalived_state,    value: BACKUP }
      - { name: keepalived_priority, value: 99 }
```

## Proxmox API Token Setup

Create the token once in Proxmox before running the playbooks:

1. Datacenter → Permissions → API Tokens → Add
2. User: `root@pam`, Token ID: `ansible`, Privilege Separation: **No**
3. Copy the displayed secret (shown only once) into vault as `vault_proxmox_api_token`

## Authentik SSO Setup

Authentik runs externally (not in the cluster). Set `vault_authentik_url` in `vars/vault.yml` before running the addons phase.

### Proxy outpost — ForwardAuth (Traefik dashboard + Longhorn)

The `authentik-forward-auth` Traefik middleware is created in the `traefik` namespace during the Traefik addon install. It forwards unauthenticated requests to:
```
{{ authentik_url }}/outpost.goauthentik.io/auth/traefik
```

In Authentik, create a **Proxy Provider** for each domain using **Forward auth (domain level)** mode, then assign each provider an Application and add both to your proxy outpost:

| Application | Domain | Redirect URI |
|---|---|---|
| Traefik | `traefik.{{ ingress_domain }}` | Handled by outpost |
| Longhorn | `longhorn.{{ ingress_domain }}` | Handled by outpost |

The Traefik and Longhorn IngressRoutes include a dedicated route for `/outpost.goauthentik.io/` that forwards callback requests directly to the outpost via an ExternalName service (with `passHostHeader: false`). This requires `allowExternalNameServices: true` in both the `kubernetesCRD` and `kubernetesIngress` Traefik providers, which is set in the Helm values. The outpost is expected to be reachable from within the cluster at `{{ authentik_url }}:9000`.

### OIDC — ArgoCD

1. In Authentik, create an **OAuth2/OIDC Provider**:
   - Client type: **Confidential**
   - Redirect URI: `https://argocd.{{ ingress_domain }}/auth/callback`
   - Scopes: `openid`, `profile`, `email`, `groups`
2. Create an **Application** with slug matching `authentik_argocd_client_id` (default: `argocd`)
3. Note the Client ID and Client Secret — add the secret to vault as `vault_authentik_argocd_client_secret`

### OIDC — Headlamp

1. In Authentik, create an **OAuth2/OIDC Provider**:
   - Client type: **Confidential**
   - Redirect URI: `https://headlamp.{{ ingress_domain }}/oidc-callback`
   - Scopes: `openid`, `profile`, `email`, `groups`, `offline_access` (`offline_access` is required for refresh tokens — without it Headlamp will loop back to the login page when the access token expires)
2. Create an **Application** with slug matching `authentik_headlamp_client_id` (default: `headlamp`)
3. Note the Client ID and Client Secret — add the secret to vault as `vault_authentik_headlamp_client_secret`
4. Import [`authentik/blueprints/headlamp-oidc.yaml`](authentik/blueprints/headlamp-oidc.yaml) (Customization > Blueprints > Import). kube-apiserver takes the username from the `email` claim, so it rejects any token without `email_verified: true` ("oidc: email not verified"). authentik has no per-user "verified" flag, and its built-in `email` scope mapping always sends `false`. The blueprint adds a mapping that sends `true` for LDAP-sourced users and swaps it in on the Headlamp provider. It is only safe while users cannot change their own email in authentik.
5. Attach the **Authentik k8s-build read-only** credential to the build job template. It injects `AUTHENTIK_READONLY_TOKEN` for authentik service account `svc-k8s-build-readonly`, which has object-level view on the Headlamp provider and the two email mappings only. The addons phase uses it to check the live config against the blueprint and fails with instructions if it has drifted. The build does not apply the blueprint itself: that needs rights to edit mappings, and mapping expressions run as Python inside authentik, so it would amount to an admin token.

The kube-apiserver is configured via a structured `AuthenticationConfiguration` file (`apiserver-auth.yaml`) that enables OIDC for Headlamp (issuer URL, client ID, username/groups claims) and restricts anonymous access to health endpoints only (`/livez`, `/readyz`, `/healthz`). This replaces individual `--oidc-*` flags, which are mutually exclusive with `--authentication-config`. The configuration is applied automatically during bootstrap and updated during the addons phase via the `patch_apiserver_oidc` task. The `headlamp_admin_group` default (`"authentik Admins"`) is bound to `cluster-admin` via a ClusterRoleBinding that covers both the Headlamp service account and OIDC group subjects.

## Prometheus Monitoring

Prometheus is deployed via [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) in the `monitoring` namespace. Grafana is disabled — metrics are visualised directly in Headlamp via the `@headlamp-k8s/prometheus-metrics` plugin.

### Components

| Component | Purpose |
|---|---|
| Prometheus | Metrics collection and storage (15-day retention, 20 Gi Longhorn PVC) |
| AlertManager | Alert routing and deduplication (2 Gi Longhorn PVC) |
| node-exporter | Per-node CPU, memory, disk, and network metrics |
| kube-state-metrics | Kubernetes object state metrics (pod status, deployment replicas, etc.) |

### Storage

Both Prometheus and AlertManager use `ReadWriteOnce` PersistentVolumeClaims on the `longhorn` StorageClass. Default sizes are set in `roles/cluster_addons/defaults/main.yml`:

```yaml
prometheus_storage_size: 20Gi
alertmanager_storage_size: 2Gi
```

### Headlamp Plugin Setup

The `@headlamp-k8s/prometheus-metrics` plugin is installed automatically via the Headlamp plugin manager sidecar. After deployment:

1. Open Headlamp and navigate to **Settings → Plugins → Prometheus Metrics**
2. Set the Prometheus URL to `https://prometheus.{{ ingress_domain }}`
3. Charts will appear on node and pod detail pages

## Longhorn Backups

Prod backs up every Longhorn volume off-cluster, to an NFS export on the
Synology (`longhorn_backup_nfs_host` / `longhorn_backup_nfs_export` in
`roles/cluster_addons/defaults/main.yml`):

- The default `BackupTarget` points at `nfs://<host>:<export>`. It's set through
  the Helm value `defaultBackupStore` and also patched on the CR, so an existing
  cluster follows the variable too.
- The `backup-daily` RecurringJob (group `default`, so every volume) runs at
  `longhorn_backup_cron` (02:00) and keeps `longhorn_backup_retain` (7) backups.
- `upgrade.yml` runs that job on demand in preflight, and waits until every
  attached volume has a backup newer than the run. It's mandatory on prod
  (`k8s_upgrade_require_backup`).
- Test clusters get no target: a throwaway cluster needs no backups, and one
  shared backupstore would mix two clusters' backup lists.

**NFS export (on the Synology):** a shared folder with an NFS rule for
`172.16.0.0/23`, read/write, squash "Map all users to admin".

**Restore:** Longhorn UI → Backup → pick the backup → Restore. Or create a
`Volume` with `spec.fromBackup` set to the backup URL.

## Descheduler

The descheduler runs as a CronJob every 6 hours in the `descheduler` namespace. It watches the cluster for pods that the initial scheduler placed sub-optimally and evicts them so they are rescheduled more evenly. The evicted pods are recreated by their controller (Deployment, DaemonSet, etc.) — no data is lost, and disruption is minimal for workloads with more than one replica.

### Strategies

| Strategy | Thresholds | Effect |
|---|---|---|
| `LowNodeUtilization` | Under-utilised: <20% cpu/mem/pods; over-utilised: >50% | Evicts pods from hot nodes so they reschedule onto underloaded ones. Requires metrics-server. |
| `RemoveDuplicates` | — | Ensures no two replicas of the same Deployment/ReplicaSet sit on the same node. |
| `RemovePodsViolatingTopologySpreadConstraints` | — | Evicts pods that violate `topologySpreadConstraints` set on their owning resource — useful for pods that were scheduled before nodes existed. |

### What is never evicted

- Pods in `kube-system`, `longhorn-system`, `metallb-system`, or `kyverno` namespaces
- Pods with PVCs attached (`ignorePvcPods: true`) — protects stateful workloads from unnecessary disruption
- Pods with a `system-cluster-critical` or `system-node-critical` priority class
- Pods using local node storage (`evictLocalStoragePods: false`)

### Tuning

Edit `roles/cluster_addons/tasks/descheduler.yml` and adjust the `LowNodeUtilization` thresholds to match your cluster's load profile:

```yaml
- name: LowNodeUtilization
  args:
    thresholds:          # evict FROM nodes above these levels
      cpu: 50
      memory: 50
      pods: 50
    targetThresholds:    # reschedule ONTO nodes below these levels
      cpu: 20
      memory: 20
      pods: 20
```

To change the schedule, update the `schedule` field (standard cron syntax). To run the descheduler on demand before the next scheduled fire:

```bash
kubectl create job --from=cronjob/descheduler descheduler-manual -n descheduler
kubectl logs -n descheduler -l job-name=descheduler-manual -f
```

## Exposed Services

After a successful run, all services are accessible via Traefik at the MetalLB LoadBalancer IP. The wildcard FreeIPA DNS record (`*.k8s` for `cluster_env=prod`, `*.k8s-test` for `cluster_env=test`) is created automatically during the addons phase — no manual DNS configuration needed for internal access.

| Service | URL | Auth |
|---|---|---|
| Traefik dashboard | `https://traefik.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Longhorn dashboard | `https://longhorn.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Prometheus | `https://prometheus.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| AlertManager | `https://alertmanager.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Headlamp dashboard | `https://headlamp.{{ ingress_domain }}` | Authentik SSO (OIDC) |
| ArgoCD | `https://argocd.{{ ingress_domain }}` | Authentik SSO (OIDC) |
| Kubernetes API | `https://{{ k8s_api_endpoint }}:6443` | kubeconfig |

`ingress_domain` is derived from `cluster_env` (`k8s.<vault_domain>` for prod, `k8s-test.<vault_domain>` for test) via `ingress_subdomain` in `vars/vms.yml`. The wildcard TLS cert covers `*.{{ ingress_domain }}`, is issued by Let's Encrypt via Cloudflare DNS-01, and is automatically mirrored to all addon namespaces by [reflector](https://github.com/emberstack/kubernetes-reflector). When cert-manager renews the cert, reflector pushes the updated secret to every namespace without any manual intervention.

The wildcard A record in FreeIPA (`*.k8s` for prod, `*.k8s-test` for test) is created automatically at the end of the addons phase. Traefik claims the first free IP from the MetalLB pool at deploy time; the actual assigned IP is read back from the service and used for the DNS record — no manual IP configuration needed. The record is removed automatically when `wipe_cluster=true`, scoped to the cluster being wiped.

Headlamp login uses Authentik OIDC — click **Sign in** on the Headlamp page and you will be redirected to Authentik. No service account token is needed.

ArgoCD login uses Authentik OIDC exclusively — the `argocd-initial-admin-secret` is automatically deleted after install. Use the **Log in via Authentik** button on the ArgoCD login page.
