# Kubernetes Cluster Provisioning

> **This was written entirely with Claude just to mess around building Kubernetes clusters in my homelab. Sharing it in case it's useful to anyone.**

Ansible automation for provisioning and managing a highly-available Kubernetes cluster on Proxmox via Foreman. Targets AlmaLinux 10, uses Flannel CNI, MetalLB, Traefik, cert-manager (Cloudflare DNS-01), Longhorn storage, Prometheus, Headlamp, ArgoCD, and Descheduler.

## Cluster Architecture

| Component | Details |
|---|---|
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
| etcd encryption | AES-CBC encryption at rest for all Secrets, configured at kubeadm init time |
| Pod Security Standards | Namespace-scoped enforcement — `privileged` for storage/network system namespaces, `baseline`/`restricted` for application namespaces |
| Policy engine | Kyverno in Audit mode — reports violations for latest image tags, missing resource limits, privileged containers, and host namespace use |
| Pod rebalancing | Descheduler CronJob (every 6 h) — evicts pods from hot nodes, spreads Deployment replicas, enforces topology spread constraints |
| Addon versions | Resolved from GitHub releases at runtime — always installs latest |

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
├── maintenance.yml       # Rolling OS maintenance (drain/update/reboot/uncordon)
├── wipe.yml              # Remove all nodes from Foreman and FreeIPA
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
│   └── vm_defaults.yml   # Default VM hardware specs
└── roles/
    ├── vm_provisioning/       # Create VMs in Foreman/Proxmox, filters by node counts
    ├── proxmox_postconfig/    # EFI disk, rename, tags via Proxmox API
    ├── enterprise_linux/      # Base OS provisioning: repos, packages, atop, DUO install
    ├── common/                # K8s prerequisites: containerd, kubelet, firewall
    ├── controlplane_infra/    # HAProxy, keepalived, controlplane firewall rules
    ├── worker_storage/        # LVM + XFS setup for Longhorn on worker data disk, iSCSI
    ├── cluster_bootstrap/     # kubeadm init/join, Flannel, kubelet-serving-cert-approver, tooling
    ├── worker_join/           # Join worker nodes to the cluster
    ├── cluster_addons/        # MetalLB, Traefik, Longhorn, reflector, cert-manager, Prometheus, Headlamp, ArgoCD, Descheduler
    ├── node_maintenance/      # Drain, update, reboot, uncordon
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

## Vault Variables

Encrypt `vars/vault.yml` with `ansible-vault encrypt vars/vault.yml`. Required keys:

| Variable | Description |
|---|---|
| `vault_proxmox_api_token` | Proxmox API token secret |
| `vault_proxmox_host` | Proxmox host and port (e.g. `pve-01.example.com:8006`) |
| `vault_proxmox_user` | Proxmox user for API auth (e.g. `user@Authentik`) |
| `vault_proxmox_node` | Proxmox node name (e.g. `pve-01`) |
| `vault_cloudflare_api_token` | Cloudflare API token for DNS-01 |
| `vault_cloudflare_email` | Cloudflare account email |
| `vault_domain` | Base domain (e.g. `example.com`) — drives `ingress_domain`, `foreman_domain`, `wildcard_secret_name`, and FreeIPA realm |
| `vault_authentik_url` | Authentik base URL (e.g. `https://auth.example.com`) |
| `vault_foreman_url` | Foreman base URL (e.g. `https://foreman.example.com`) |
| `vault_etcd_encryption_key` | Base64-encoded 32-byte key for etcd AES-CBC encryption at rest |
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

Generate the etcd encryption key once and store it in the vault — **do not change it after the cluster is provisioned** (doing so requires a full secret re-encryption rotation):

```bash
head -c 32 /dev/urandom | base64
```

## AWX Survey Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `wipe_cluster` | Boolean | `false` | When `true`, removes all existing cluster nodes from Foreman and FreeIPA first, then provisions a fresh cluster using the current `controlplane_node_count` and `worker_node_count` values. |
| `controlplane_node_count` | Integer | 1 | Number of control plane nodes to provision this run. Set to `0` when only adding worker nodes — this skips the bootstrap and cluster add-ons phases entirely. Max 3. |
| `worker_node_count` | Integer | 1 | Number of worker nodes to add this run. The provisioning role queries Foreman for existing workers with the configured prefix and starts numbering from the next available index. Running with `worker_node_count=3` twice produces 6 workers total. |
| `cluster_env` | String | `prod` | `prod` or `test`. Controls node naming prefixes, ingress domain, FreeIPA wildcard DNS record, and ACME endpoint. `prod` → `k8s-cp` / `k8s-worker`, `k8s.yourdomain.com`, Let's Encrypt production. `test` → `k8s-test-cp` / `k8s-test-worker`, `k8s-test.yourdomain.com`, Let's Encrypt staging. Both clusters can run side-by-side — they use separate DNS records and name prefixes. |
| `letsencrypt_staging` | Boolean | derived | Auto-derived from `cluster_env` (`true` when `test`, `false` when `prod`). When `true`, uses the Let's Encrypt **staging** ACME endpoint — certs are not browser-trusted but have no rate limits. Only set this directly if you need to decouple it from `cluster_env`. |
| `k8s_api_endpoint` | String | `k8s-api.example.com` | DNS name for the Kubernetes API endpoint (the keepalived VIP hostname). An A record pointing to `k8s_api_endpoint_ip` is created in FreeIPA during the provision phase and removed on wipe. |
| `k8s_api_endpoint_ip` | String | `172.16.0.29` | IP address of the keepalived VIP. Also used as `keepalived_vip` — only set this here, do not update them separately. |
| `metallb_pool` | String | `172.16.0.50-172.16.0.60` | MetalLB L2 address pool range for LoadBalancer services. Traefik automatically claims the first free IP from this pool. |
| `k8s_pod_cidr` | String | `10.244.0.0/16` | Pod network CIDR passed to kubeadm and used in firewall rules. Must not overlap with your node or service networks. Change only if the default conflicts with your infrastructure. |
| `k8s_service_cidr` | String | `10.96.0.0/12` | Kubernetes service network CIDR passed to kubeadm and used in firewall rules and NetworkPolicies. Must not overlap with your node or pod networks. Change only if the default conflicts with your infrastructure. |

`wipe_cluster=true` runs a full rebuild: existing nodes are removed from Foreman and FreeIPA, then provisioning continues immediately with the rest of the survey values (`controlplane_node_count`, `worker_node_count`). The Foreman queries in the provisioning role will see zero existing nodes after the wipe, so numbering restarts from `01`.

Neither control plane nor worker nodes have hardcoded names. Both are generated at runtime from a prefix and a counter. The prefix is derived from `cluster_env`: `prod` produces `k8s-cp` / `k8s-worker`; `test` produces `k8s-test-cp` / `k8s-test-worker`. Foreman hostgroup strings are configured in `vars/vms.yml`.

Prod and test clusters can run side-by-side. Each has its own FreeIPA DNS wildcard (`*.k8s` vs `*.k8s-test`), so wiping one does not affect the other's ingress. You must set non-overlapping values for `k8s_api_endpoint_ip` and `metallb_pool` in each cluster's AWX survey — those IP ranges cannot be shared.

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

Drains the node, removes it from Kubernetes, resets kubeadm state on the VM itself, then deletes the host from Foreman and FreeIPA. Pass one or more short hostnames (without the domain suffix):

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

Drains, updates packages, reboots, and uncordons nodes. Control planes are rolled one at a time; workers two at a time.

```bash
ansible-playbook maintenance.yml --ask-vault-pass

# Workers only
ansible-playbook maintenance.yml --limit foreman_almalinux10_kubernetesworkernode

# Override drain timeout
ansible-playbook maintenance.yml -e "drain_timeout=600"
```

### Wipe Cluster

Queries Foreman for all hosts matching the configured CP and worker prefixes and removes them from Foreman and FreeIPA. Finds everything regardless of how many scale-out runs were done. Does not touch the Proxmox VMs themselves.

```bash
ansible-playbook wipe.yml --ask-vault-pass
```

## VM Definitions

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
  # k8s_api_endpoint, k8s_api_endpoint_ip, metallb_pool, k8s_pod_cidr, and
  # k8s_service_cidr are AWX survey vars injected at runtime — set them in
  # your job template survey rather than hardcoding them here.
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
4. In Authentik, ensure the user's email is marked as **verified** — kube-apiserver rejects OIDC tokens where `email_verified: false` when `--oidc-username-claim=email` is set

The kube-apiserver is configured with OIDC flags (`--oidc-issuer-url`, `--oidc-client-id`, `--oidc-username-claim=email`, `--oidc-groups-claim=groups`) so that Headlamp can make Kubernetes API calls using the user's OIDC token directly. This configuration is applied automatically during the addons phase via the `patch_apiserver_oidc` task, which patches the static pod manifest on all control plane nodes and waits for the apiserver to restart. The `headlamp_admin_group` default (`"authentik Admins"`) is bound to `cluster-admin` via a ClusterRoleBinding that covers both the Headlamp service account and OIDC group subjects.

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

The `@headlamp-k8s/prometheus-metrics` plugin is installed automatically at Headlamp pod startup via an init container. After deployment:

1. Open Headlamp and navigate to **Settings → Plugins → Prometheus Metrics**
2. Set the Prometheus URL to `https://prometheus.{{ ingress_domain }}`
3. Charts will appear on node and pod detail pages

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

After a successful run, all services are accessible via Traefik at the MetalLB LoadBalancer IP. The `*.k8s` FreeIPA DNS record is created automatically during the addons phase — no manual DNS configuration needed for internal access.

| Service | URL | Auth |
|---|---|---|
| Traefik dashboard | `https://traefik.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Longhorn dashboard | `https://longhorn.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Prometheus | `https://prometheus.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| AlertManager | `https://alertmanager.{{ ingress_domain }}` | Authentik SSO (ForwardAuth) |
| Headlamp dashboard | `https://headlamp.{{ ingress_domain }}` | Authentik SSO (OIDC) |
| ArgoCD | `https://argocd.{{ ingress_domain }}` | Authentik SSO (OIDC) |
| Kubernetes API | `https://{{ k8s_api_endpoint }}:6443` | kubeconfig |

`ingress_domain` defaults to `k8s.<domain>` and is configured in `roles/cluster_addons/defaults/main.yml`. The wildcard TLS cert covers `*.{{ ingress_domain }}`, is issued by Let's Encrypt via Cloudflare DNS-01, and is automatically mirrored to all addon namespaces by [reflector](https://github.com/emberstack/kubernetes-reflector). When cert-manager renews the cert, reflector pushes the updated secret to every namespace without any manual intervention.

The `*.k8s` wildcard A record in FreeIPA is created automatically at the end of the addons phase. Traefik claims the first free IP from the MetalLB pool at deploy time; the actual assigned IP is read back from the service and used for the DNS record — no manual IP configuration needed. The record is removed automatically when `wipe_cluster=true`.

Headlamp login uses Authentik OIDC — click **Sign in** on the Headlamp page and you will be redirected to Authentik. No service account token is needed.

Retrieve the ArgoCD initial admin password after the addons run completes:

```bash
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d
```

Change the password after first login — ArgoCD deletes the `argocd-initial-admin-secret` once the password is updated, which is expected.
