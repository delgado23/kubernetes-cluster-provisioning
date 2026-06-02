#!/usr/bin/env python3
"""Generate the cluster-provisioning pipeline diagram in four formats.

Produces, in the repository root, from a single node/edge model derived from
`main.yml`:

    k8s-cluster-provisioning-process.vsdx   native Microsoft Visio (editable)
    k8s-cluster-provisioning-process.svg    vector source
    k8s-cluster-provisioning-process.html   self-contained, embeds the SVG
    k8s-cluster-provisioning-process.pdf    150-DPI raster (needs Pillow)

Stdlib only for .vsdx/.svg/.html; the .pdf step additionally uses Pillow and a
system TrueType font (Noto Sans by default). Run after the pipeline changes to
keep the diagram in sync:

    python3 docs/generate_diagram.py
"""
import math, zipfile, html, os

# Output next to the repo root (parent of this docs/ directory)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "k8s-cluster-provisioning-process")

PAGE_W, PAGE_H = 24.0, 34.0

# ---- palette: (fill, line, text) ----------------------------------------
PHASE  = ("#DEEBF7", "#2E75B6", "#1F3864")  # ansible pipeline (blue)
ORCH   = ("#FFF2CC", "#BF9000", "#7F6000")  # ascender/awx (gold)
INFRA  = ("#FBE5D6", "#C55A11", "#833C04")  # foreman / proxmox (orange)
IDENT  = ("#E2EFDA", "#538135", "#2E5018")  # freeipa (green)
SEC    = ("#F4CCCC", "#CC0000", "#800000")  # duo (red, 2FA)
SSO    = ("#EAD1DC", "#843C7C", "#4C2347")  # authentik (purple)
CLOUD  = ("#FCE5CD", "#E69138", "#7F4F11")  # cloudflare (amber)
RESULT = ("#D9EAD3", "#38761D", "#1E4010")  # finished cluster (green)
PLAIN  = (None, None, "#000000")            # title / labels
NEUTRAL = ("#FFFFFF", "#808080", "#404040")  # operator

nodes = {}   # id -> dict
edges = []   # (src, dst, label)


def box(id, cx, cy, w, h, text, colors, size=0.12, bold=False):
    nodes[id] = dict(cx=cx, cy=cy, w=w, h=h, text=text,
                     fill=colors[0], line=colors[1], txt=colors[2],
                     size=size, bold=bold)


def e(s, d, label=""):
    edges.append((s, d, label))


def build_model():
    # title
    box("TITLE", 12, 0.85, 18, 0.7,
        "Kubernetes HA Cluster Provisioning Pipeline", PLAIN, size=0.26, bold=True)
    box("SUB", 12, 1.55, 18, 0.5,
        "Ansible (main.yml) orchestrated by Ascender/AWX  —  AlmaLinux 10 on Proxmox via Foreman",
        PLAIN, size=0.13)

    # left column : orchestration / identity / security / sso
    LX = 4.2
    box("OP",   LX, 2.6, 3.0, 0.9, "Operator\n(launch job + survey)", NEUTRAL, size=0.12)
    box("ASC",  LX, 4.6, 4.6, 2.0,
        "Ascender / AWX\nJob Template + Survey\ncontrolplane_node_count\nworker_node_count\n"
        "cluster_env, wipe_cluster\n(AWX API: autoscale schedules)", ORCH, size=0.115)
    box("IPA",  LX, 10.3, 4.6, 2.2,
        "FreeIPA\n• ipaclient host enrollment\n• Kerberos identity\n"
        "• DNS: k8s-api A record\n• DNS: wildcard *.k8s", IDENT, size=0.115)
    box("DUO",  LX, 13.7, 4.6, 1.5,
        "Duo Security\nSSH 2FA (login_duo)\nForceCommand on sshd", SEC, size=0.12)
    box("AUTH", LX, 24.0, 4.6, 2.0,
        "Authentik (external)\nSSO for cluster apps\n• ForwardAuth (Traefik,\n  Longhorn, Prometheus)\n"
        "• OIDC (ArgoCD, Headlamp)", SSO, size=0.115)

    # right column : infra / cloudflare
    RX = 20.0
    box("FM",   RX, 8.0, 4.8, 1.9,
        "Foreman\n• Host/VM provisioning\n• Proxmox compute resource\n• Static inventory source",
        INFRA, size=0.115)
    box("PVE",  RX, 10.6, 4.8, 1.9,
        "Proxmox VE\n• Creates the VMs\n• API postconfig: EFI disk,\n  rename, tags, CPU/mem hotplug",
        INFRA, size=0.115)
    box("CF",   RX, 22.0, 4.8, 1.7,
        "Cloudflare\nDNS-01 ACME challenge\nLet's Encrypt wildcard cert", CLOUD, size=0.115)

    # center column : the ansible pipeline phases
    CX = 12.0
    box("C1", CX, 3.4, 5.6, 1.0, "Ansible Control Node\nmain.yml pipeline", PHASE, size=0.135, bold=True)
    box("C2", CX, 5.2, 5.6, 1.0, "1.  Disable autoscale schedules\n(AWX API, this cluster_env)", PHASE)
    box("C3", CX, 7.0, 5.6, 1.1, "2.  (Optional) WIPE\nremove old nodes from\nForeman + FreeIPA", PHASE)
    box("C4", CX, 9.4, 6.0, 1.9,
        "3.  PROVISION\n• Create hosts via Foreman → Proxmox\n• Proxmox postconfig (hotplug, EFI)\n"
        "• FreeIPA k8s-api DNS\n• Wait for SSH", PHASE, size=0.115)
    box("C5", CX, 13.1, 6.2, 2.6,
        "4.  PREP (per node, idempotent)\n• enterprise_linux: OS, repos, Duo install\n"
        "• FreeIPA ipaclient enroll + reboot\n• common: containerd, kubelet, firewall\n"
        "• controlplane_infra: HAProxy + keepalived\n• worker_storage: LVM/XFS Longhorn", PHASE, size=0.115)
    box("C6", CX, 16.9, 6.2, 2.6,
        "5.  BOOTSTRAP (control plane)\n• kubeadm init primary\n  (etcd encryption, OIDC auth config)\n"
        "• join secondary control planes\n• audit logging, kubelet-cert-approver\n"
        "• tooling: kubectl, Helm, k9s", PHASE, size=0.115)
    box("C7", CX, 20.0, 5.6, 1.1, "6.  WORKERS\nkubeadm join workers + label", PHASE)
    box("C8", CX, 23.2, 6.4, 2.9,
        "7.  ADDONS (primary CP)\nKyverno • metrics-server • descheduler\nMetalLB • kube-router • Longhorn\n"
        "reflector • cert-manager • Prometheus\nTraefik • Headlamp • ArgoCD • PSS\n"
        "→ FreeIPA wildcard ingress DNS", PHASE, size=0.115)
    box("C9", CX, 26.6, 5.6, 1.0, "8.  Enable autoscale schedules\n(AWX API)", PHASE)
    box("C10", CX, 28.9, 6.6, 1.7,
        "Kubernetes HA Cluster\ncontrol plane (HAProxy+keepalived VIP)\n+ worker nodes  —  ready",
        RESULT, size=0.13, bold=True)

    # main vertical chain
    for a, b in [("C1", "C2"), ("C2", "C3"), ("C3", "C4"), ("C4", "C5"), ("C5", "C6"),
                 ("C6", "C7"), ("C7", "C8"), ("C8", "C9"), ("C9", "C10")]:
        e(a, b)
    # orchestration
    e("OP", "ASC", "launch"); e("ASC", "C1", "run playbook")
    e("C2", "ASC", "disable"); e("C9", "ASC", "enable")
    # provisioning / infra
    e("C3", "FM", "remove"); e("C3", "IPA", "remove")
    e("C4", "FM", "create hosts"); e("FM", "PVE", "provision VM")
    e("C4", "PVE", "postconfig API"); e("C4", "IPA", "k8s-api DNS")
    # prep / identity / security
    e("C5", "IPA", "enroll"); e("C5", "DUO", "install 2FA")
    # addons / certs / dns / sso
    e("C8", "CF", "DNS-01"); e("C8", "IPA", "wildcard DNS"); e("C8", "AUTH", "wire SSO")
    # finished cluster integrations
    e("C10", "AUTH", "app login"); e("C10", "DUO", "node SSH")


# ===========================================================================
# geometry helpers
# ===========================================================================
def clip(n, tx, ty):
    """point on box n's border toward (tx,ty), in logical (top-left) coords"""
    cx, cy, hw, hh = n["cx"], n["cy"], n["w"] / 2, n["h"] / 2
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = hw / abs(dx) if dx else float("inf")
    sy = hh / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


def L2V(x, y):  # logical (y-down) -> visio (y-up)
    return x, PAGE_H - y


def esc(t):
    return html.escape(t, quote=True)


# ===========================================================================
# .vsdx  (OPC / ZIP package of XML parts)
# ===========================================================================
NS_MAIN = "http://schemas.microsoft.com/office/visio/2012/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def write_vsdx():
    shapes = []
    sid = [0]

    def emit_box(n):
        sid[0] += 1
        px, py = L2V(n["cx"], n["cy"])
        w, h = n["w"], n["h"]
        style = "1" if n["bold"] else "0"
        if n["fill"]:
            fill = (f'<Cell N="FillForegnd" V="{n["fill"]}"/><Cell N="FillBkgnd" V="{n["fill"]}"/>'
                    f'<Cell N="LineColor" V="{n["line"]}"/><Cell N="LineWeight" V="0.013"/>'
                    f'<Cell N="Rounding" V="0.12"/>')
            geom = ('<Section N="Geometry" IX="0"><Cell N="NoFill" V="0"/>'
                    '<Row T="RelMoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
                    '<Row T="RelLineTo" IX="2"><Cell N="X" V="1"/><Cell N="Y" V="0"/></Row>'
                    '<Row T="RelLineTo" IX="3"><Cell N="X" V="1"/><Cell N="Y" V="1"/></Row>'
                    '<Row T="RelLineTo" IX="4"><Cell N="X" V="0"/><Cell N="Y" V="1"/></Row>'
                    '<Row T="RelLineTo" IX="5"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row></Section>')
        else:
            fill = '<Cell N="LinePattern" V="0"/><Cell N="FillPattern" V="0"/>'
            geom = ''
        char = (f'<Section N="Character"><Row IX="0"><Cell N="Size" V="{n["size"]}"/>'
                f'<Cell N="Color" V="{n["txt"]}"/><Cell N="Style" V="{style}"/></Row></Section>')
        para = '<Section N="Paragraph"><Row IX="0"><Cell N="HorzAlign" V="1"/></Row></Section>'
        shapes.append(
            f'<Shape ID="{sid[0]}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">'
            f'<Cell N="PinX" V="{px:.4f}"/><Cell N="PinY" V="{py:.4f}"/>'
            f'<Cell N="Width" V="{w:.4f}"/><Cell N="Height" V="{h:.4f}"/>'
            f'<Cell N="LocPinX" F="Width*0.5" V="{w/2:.4f}"/>'
            f'<Cell N="LocPinY" F="Height*0.5" V="{h/2:.4f}"/>'
            f'<Cell N="VerticalAlign" V="1"/>{fill}{geom}{char}{para}'
            f'<Text>{esc(n["text"])}</Text></Shape>')

    def emit_edge(s, d, label):
        sid[0] += 1
        ns, nd = nodes[s], nodes[d]
        bx, by = clip(ns, nd["cx"], nd["cy"])
        ex, ey = clip(nd, ns["cx"], ns["cy"])
        bxv, byv = L2V(bx, by)
        exv, eyv = L2V(ex, ey)
        dx, dy = exv - bxv, eyv - byv
        length = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        pinx, piny = (bxv + exv) / 2, (byv + eyv) / 2
        geom = ('<Section N="Geometry" IX="0"><Cell N="NoFill" V="1"/>'
                '<Row T="MoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="0"/></Row>'
                f'<Row T="LineTo" IX="2"><Cell N="X" V="{length:.4f}"/><Cell N="Y" V="0"/></Row></Section>')
        shapes.append(
            f'<Shape ID="{sid[0]}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">'
            f'<Cell N="PinX" V="{pinx:.4f}"/><Cell N="PinY" V="{piny:.4f}"/>'
            f'<Cell N="Width" V="{length:.4f}"/><Cell N="Height" V="0"/>'
            f'<Cell N="LocPinX" F="Width*0.5" V="{length/2:.4f}"/><Cell N="LocPinY" V="0"/>'
            f'<Cell N="Angle" V="{angle:.6f}"/>'
            f'<Cell N="LineColor" V="#5A5A5A"/><Cell N="LineWeight" V="0.014"/>'
            f'<Cell N="EndArrow" V="4"/><Cell N="EndArrowSize" V="2"/>{geom}</Shape>')
        if label:
            sid[0] += 1
            mx, my = (bx + ex) / 2, (by + ey) / 2
            lpx, lpy = L2V(mx, my)
            shapes.append(
                f'<Shape ID="{sid[0]}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">'
                f'<Cell N="PinX" V="{lpx:.4f}"/><Cell N="PinY" V="{lpy:.4f}"/>'
                f'<Cell N="Width" V="1.3"/><Cell N="Height" V="0.28"/>'
                f'<Cell N="LocPinX" V="0.65"/><Cell N="LocPinY" V="0.14"/>'
                f'<Cell N="LinePattern" V="0"/><Cell N="FillPattern" V="0"/>'
                f'<Cell N="VerticalAlign" V="1"/>'
                f'<Section N="Character"><Row IX="0"><Cell N="Size" V="0.1"/>'
                f'<Cell N="Color" V="#404040"/><Cell N="Style" V="2"/></Row></Section>'
                f'<Section N="Paragraph"><Row IX="0"><Cell N="HorzAlign" V="1"/></Row></Section>'
                f'<Text>{esc(label)}</Text></Shape>')

    for s, d, label in edges:   # edges first so they sit behind boxes
        emit_edge(s, d, label)
    for n in nodes.values():
        emit_box(n)

    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
            '<Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>'
            '<Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>',
        "docProps/core.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>Kubernetes Cluster Provisioning Pipeline</dc:title>'
            '<dc:creator>generate_diagram.py</dc:creator>'
            '<cp:lastModifiedBy>generate_diagram.py</cp:lastModifiedBy></cp:coreProperties>',
        "docProps/app.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            '<Application>Microsoft Visio</Application><Company></Company></Properties>',
        "visio/document.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<VisioDocument xmlns="{NS_MAIN}" xmlns:r="{NS_REL}" xml:space="preserve">'
            '<DocumentSettings TopPage="0" DefaultTextStyle="0">'
            '<GlueSettings>9</GlueSettings><SnapSettings>65463</SnapSettings>'
            '<SnapExtensions>34</SnapExtensions><DynamicGridEnabled>1</DynamicGridEnabled>'
            '<ProtectStyles>0</ProtectStyles><ProtectShapes>0</ProtectShapes>'
            '<ProtectMasters>0</ProtectMasters><ProtectBkgnds>0</ProtectBkgnds></DocumentSettings>'
            '<Colors/><FaceNames/><StyleSheets/>'
            '<DocumentSheet NameU="TheDoc" LineStyle="0" FillStyle="0" TextStyle="0"/></VisioDocument>',
        "visio/_rels/document.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/pages" Target="pages/pages.xml"/>'
            '</Relationships>',
        "visio/pages/pages.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Pages xmlns="{NS_MAIN}" xmlns:r="{NS_REL}" xml:space="preserve">'
            '<Page ID="0" NameU="Pipeline" Name="Pipeline" ViewScale="-1" ViewCenterX="12" ViewCenterY="17">'
            '<PageSheet LineStyle="0" FillStyle="0" TextStyle="0">'
            f'<Cell N="PageWidth" V="{PAGE_W}"/><Cell N="PageHeight" V="{PAGE_H}"/>'
            '<Cell N="ShdwOffsetX" V="0.0833333333333333"/><Cell N="ShdwOffsetY" V="-0.0833333333333333"/>'
            '<Cell N="PageScale" V="1" U="IN_F"/><Cell N="DrawingScale" V="1" U="IN_F"/>'
            '<Cell N="DrawingSizeType" V="3"/><Cell N="DrawingScaleType" V="0"/>'
            '<Cell N="InhibitSnap" V="0"/><Cell N="PageLockReplace" V="0"/>'
            '<Cell N="PageLockDuplicate" V="0"/><Cell N="UIVisibility" V="0"/></PageSheet>'
            '<Rel r:id="rId1"/></Page></Pages>',
        "visio/pages/_rels/pages.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/page" Target="page1.xml"/>'
            '</Relationships>',
        "visio/pages/page1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<PageContents xmlns="{NS_MAIN}" xmlns:r="{NS_REL}" xml:space="preserve">'
            '<Shapes>' + ''.join(shapes) + '</Shapes></PageContents>',
    }
    out = BASE + ".vsdx"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return out


# ===========================================================================
# .svg  +  .html
# ===========================================================================
def build_svg():
    PX = 72.0
    W, H = PAGE_W * PX, PAGE_H * PX

    def X(v):
        return v * PX

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
         f'font-family="Segoe UI, Helvetica, Arial, sans-serif">',
         '<defs><marker id="arr" markerWidth="10" markerHeight="10" refX="8" refY="3" '
         'orient="auto" markerUnits="userSpaceOnUse" viewBox="0 0 10 6">'
         '<path d="M0,0 L9,3 L0,6 z" fill="#5A5A5A"/></marker></defs>',
         f'<rect x="0" y="0" width="{W:.0f}" height="{H:.0f}" fill="white"/>']
    for s, dd, lab in edges:
        ns, nd = nodes[s], nodes[dd]
        bx, by = clip(ns, nd["cx"], nd["cy"])
        ex, ey = clip(nd, ns["cx"], ns["cy"])
        p.append(f'<line x1="{X(bx):.1f}" y1="{X(by):.1f}" x2="{X(ex):.1f}" y2="{X(ey):.1f}" '
                 f'stroke="#5A5A5A" stroke-width="1.6" marker-end="url(#arr)"/>')
        if lab:
            mx, my = (bx + ex) / 2, (by + ey) / 2
            w = len(lab) * 5.6 + 8
            p.append(f'<rect x="{X(mx)-w/2:.1f}" y="{X(my)-8:.1f}" width="{w:.1f}" height="16" '
                     f'fill="white" opacity="0.92"/>')
            p.append(f'<text x="{X(mx):.1f}" y="{X(my):.1f}" font-size="9.5" fill="#404040" '
                     f'font-style="italic" text-anchor="middle" dominant-baseline="central">{esc(lab)}</text>')
    for n in nodes.values():
        x0, y0 = X(n["cx"] - n["w"] / 2), X(n["cy"] - n["h"] / 2)
        bw, bh = X(n["w"]), X(n["h"])
        if n["fill"]:
            p.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="7" '
                     f'fill="{n["fill"]}" stroke="{n["line"]}" stroke-width="1.6"/>')
        lines = n["text"].split("\n")
        fs = n["size"] * PX * 0.95
        lh = fs * 1.32
        cx, cy = X(n["cx"]), X(n["cy"])
        start = cy - (len(lines) - 1) / 2 * lh
        weight = "700" if n["bold"] else "400"
        p.append(f'<text x="{cx:.1f}" y="{start:.1f}" font-size="{fs:.1f}" fill="{n["txt"]}" '
                 f'font-weight="{weight}" text-anchor="middle" dominant-baseline="central">')
        for i, ln in enumerate(lines):
            dy = 0 if i == 0 else lh
            p.append(f'<tspan x="{cx:.1f}" dy="{dy:.1f}">{esc(ln)}</tspan>')
        p.append('</text>')
    p.append('</svg>')
    return "\n".join(p)


def write_svg_html(svg):
    with open(BASE + ".svg", "w") as f:
        f.write(svg)
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kubernetes Cluster Provisioning Pipeline</title>
<style>
 body{{margin:0;background:#f4f5f7;font-family:'Segoe UI',Helvetica,Arial,sans-serif;color:#222}}
 header{{padding:18px 24px;background:#1F3864;color:#fff}}
 header h1{{margin:0;font-size:20px}} header p{{margin:4px 0 0;font-size:13px;opacity:.85}}
 .wrap{{padding:20px;display:flex;justify-content:center}}
 .diagram{{background:#fff;border:1px solid #d0d4da;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.08);padding:12px;max-width:100%}}
 .diagram svg{{width:100%;height:auto;max-width:960px}}
 footer{{text-align:center;font-size:12px;color:#888;padding:14px}}
</style></head>
<body>
<header><h1>Kubernetes HA Cluster Provisioning Pipeline</h1>
<p>Ansible (main.yml) orchestrated by Ascender/AWX — Foreman · Proxmox · FreeIPA · Duo · Cloudflare · Authentik</p></header>
<div class="wrap"><div class="diagram">
{svg}
</div></div>
<footer>Generated by docs/generate_diagram.py — also available as .vsdx (Visio) and .pdf</footer>
</body></html>"""
    with open(BASE + ".html", "w") as f:
        f.write(doc)
    return BASE + ".svg", BASE + ".html"


# ===========================================================================
# .pdf  (high-DPI raster; requires Pillow + a system TrueType font)
# ===========================================================================
FONT_CANDIDATES = [
    "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def write_pdf(dpi=150):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  (skipping .pdf — Pillow not installed)")
        return None
    font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)

    def fnt(sz, bold=False):
        if not font_path:
            return ImageFont.load_default()
        f = ImageFont.truetype(font_path, sz)
        if bold:
            try:
                f.set_variation_by_axes([700])
            except Exception:
                pass
        return f

    S = dpi
    img = Image.new("RGB", (int(PAGE_W * S), int(PAGE_H * S)), "white")
    d = ImageDraw.Draw(img)
    for s, dd, lab in edges:
        ns, nd = nodes[s], nodes[dd]
        bx, by = clip(ns, nd["cx"], nd["cy"])
        ex, ey = clip(nd, ns["cx"], ns["cy"])
        d.line([bx * S, by * S, ex * S, ey * S], fill="#5A5A5A", width=3)
        ang = math.atan2(ey - by, ex - bx)
        for da in (0.4, -0.4):
            d.line([ex * S, ey * S, (ex - 0.25 * math.cos(ang - da)) * S,
                    (ey - 0.25 * math.sin(ang - da)) * S], fill="#5A5A5A", width=3)
        if lab:
            mx, my = (bx + ex) / 2, (by + ey) / 2
            f = fnt(int(0.12 * S))
            tb = d.textbbox((0, 0), lab, font=f)
            d.rectangle([mx * S - (tb[2] - tb[0]) / 2 - 3, my * S - 10,
                         mx * S + (tb[2] - tb[0]) / 2 + 3, my * S + 10], fill="white")
            d.text((mx * S, my * S), lab, font=f, fill="#404040", anchor="mm")
    for n in nodes.values():
        x0, y0 = (n["cx"] - n["w"] / 2) * S, (n["cy"] - n["h"] / 2) * S
        x1, y1 = (n["cx"] + n["w"] / 2) * S, (n["cy"] + n["h"] / 2) * S
        if n["fill"]:
            d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=n["fill"], outline=n["line"], width=3)
        f = fnt(int(n["size"] * S * 0.95), n["bold"])
        d.multiline_text(((x0 + x1) / 2, (y0 + y1) / 2), n["text"], font=f,
                         fill=n["txt"], anchor="mm", align="center", spacing=4)
    out = BASE + ".pdf"
    img.save(out, "PDF", resolution=dpi)
    return out


def main():
    build_model()
    written = [write_vsdx()]
    svg = build_svg()
    written.extend(write_svg_html(svg))
    pdf = write_pdf()
    if pdf:
        written.append(pdf)
    print("Generated %d shapes (%d nodes, %d edges):" % (
        len(nodes) + len(edges), len(nodes), len(edges)))
    for w in written:
        print("  " + os.path.relpath(w, ROOT))


if __name__ == "__main__":
    main()
