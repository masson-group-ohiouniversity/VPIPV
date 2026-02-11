import numpy as np
import math
import streamlit as st
import plotly.graph_objects as go
from scipy.optimize import least_squares

st.set_page_config(page_title="VPIPV Simulator", layout="wide")

from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

def snap_logk_input(label: str, key: str, value: float, disabled: bool = False):
    """LogK input with minimal custom +/- that snaps to the nearest 0.1 grid before stepping.
    Only used for equilibrium constants to avoid 'buttons all over'.
    """
    if key not in st.session_state:
        st.session_state[key] = float(value)

    def _to_dec(x: float) -> Decimal:
        return Decimal(str(x))

    def _on_grid(x: float) -> bool:
        d = _to_dec(x)
        return (d * Decimal("10")) == (d * Decimal("10")).to_integral_value()

    def _snap_up(x: float) -> float:
        d = _to_dec(x)
        return float(((d * Decimal("10")).to_integral_value(rounding=ROUND_CEILING)) / Decimal("10"))

    def _snap_down(x: float) -> float:
        d = _to_dec(x)
        return float(((d * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR)) / Decimal("10"))

    c1, c2, c3 = st.columns([8, 1, 1], gap="small")
    with c1:
        v = st.number_input(label, value=float(st.session_state[key]), step=0.0, format="%.2f",
                            disabled=disabled, key=f"{key}__box")
        st.session_state[key] = float(v)

    with c2:
        if st.button("−", key=f"{key}__minus", disabled=disabled):
            x = float(st.session_state[key])
            x_new = (x - 0.1) if _on_grid(x) else _snap_down(x)
            st.session_state[key] = float(x_new)

    with c3:
        if st.button("+", key=f"{key}__plus", disabled=disabled):
            x = float(st.session_state[key])
            x_new = (x + 0.1) if _on_grid(x) else _snap_up(x)
            st.session_state[key] = float(x_new)

    return float(st.session_state[key])


# -----------------------------
# Chemistry model helpers
# -----------------------------
def pow10(x: float) -> float:
    return 10.0 ** x

def coeffs_G(H: float, M: float, p: dict) -> dict:
    """
    Coefficients multiplying G_free for each G-species:
      [species] = coeff * G_free

    Network (11 equilibria + optional GH3):
      G, GM, GM2
      GH, GHM, GHM2
      GH2, GH2M, GH2M2
      GH3 (optional), GH3M, GH3M2

    with special steps:
      GH2M  + H <-> GH3M   (KH3M)
      GH2M2 + H <-> GH3M2  (KH3M2)
    """
    KM0  = pow10(p["logKM0"])
    KM20 = pow10(p["logKM20"])
    KM1  = pow10(p["logKM1"])
    KM21 = pow10(p["logKM21"])
    KM2  = pow10(p["logKM2"])
    KM22 = pow10(p["logKM22"])

    KH1  = pow10(p["logKH1"])
    KH2  = pow10(p["logKH2"])
    KH3  = pow10(p["logKH3"]) if p["allow_KH3"] else 0.0

    KH3M  = pow10(p["logKH3M"])
    KH3M2 = pow10(p["logKH3M2"])

    # Level 0
    c_G   = 1.0
    c_GM  = KM0 * M
    c_GM2 = KM0 * KM20 * M * M

    # Level 1
    c_GH   = KH1 * H
    c_GHM  = c_GH * KM1 * M
    c_GHM2 = c_GH * KM1 * KM21 * M * M

    # Level 2
    c_GH2   = (KH1 * KH2) * H * H
    c_GH2M  = c_GH2 * KM2 * M
    c_GH2M2 = c_GH2 * KM2 * KM22 * M * M

    # Level 3 (optional metal-free GH3)
    c_GH3 = 0.0 if KH3 == 0.0 else (KH1 * KH2 * KH3) * H * H * H

    # Metalated level-3 via special steps
    c_GH3M  = c_GH2M  * KH3M  * H
    c_GH3M2 = c_GH2M2 * KH3M2 * H

    return {
        "G": c_G,
        "GM": c_GM,
        "GM2": c_GM2,
        "GH": c_GH,
        "GHM": c_GHM,
        "GHM2": c_GHM2,
        "GH2": c_GH2,
        "GH2M": c_GH2M,
        "GH2M2": c_GH2M2,
        "GH3": c_GH3,
        "GH3M": c_GH3M,
        "GH3M2": c_GH3M2,
    }

def q_partition(H: float, M: float, Qtot: float, p: dict):
    """
    Optional competitor Q network:
      Q + H  <-> QH   with KQ
      Q + M  <-> QM   with KQM
      QH + M <-> QHM  with KQHM

    Binding polynomial: denom = 1 + KQ*H + KQM*M + (KQ*KQHM)*H*M
    """
    if (not p["enable_Q"]) or Qtot <= 0.0:
        return dict(Qfree=Qtot, QH=0.0, QM=0.0, QHM=0.0, HboundQ=0.0, MboundQ=0.0)

    KQ   = pow10(p["logKQ"])
    KQM  = pow10(p["logKQM"])
    KQHM = pow10(p["logKQHM"])

    denom = 1.0 + KQ * H + KQM * M + (KQ * KQHM) * H * M
    Qfree = Qtot / denom
    QH  = KQ * Qfree * H
    QM  = KQM * Qfree * M
    QHM = (KQ * KQHM) * Qfree * H * M

    HboundQ = QH + QHM
    MboundQ = QM + QHM
    return dict(Qfree=Qfree, QH=QH, QM=QM, QHM=QHM, HboundQ=HboundQ, MboundQ=MboundQ)

def solve_free_HM(Gtot: float, Htot: float, Mtot: float, Qtot: float, p: dict, x0_lnHM: np.ndarray):
    """
    Solve mass balances for free H and free M using SciPy least_squares in log-space.
    Variables: lnH, lnM (so H,M are positive).
    """
    def residuals(x):
        lnH, lnM = x
        H = np.exp(lnH)
        M = np.exp(lnM)

        c = coeffs_G(H, M, p)
        Gfree = Gtot / sum(c.values())

        HboundG = Gfree * (
            1*(c["GH"] + c["GHM"] + c["GHM2"]) +
            2*(c["GH2"] + c["GH2M"] + c["GH2M2"]) +
            3*(c["GH3"] + c["GH3M"] + c["GH3M2"])
        )

        MboundG = Gfree * (
            1*(c["GM"] + c["GHM"] + c["GH2M"] + c["GH3M"]) +
            2*(c["GM2"] + c["GHM2"] + c["GH2M2"] + c["GH3M2"])
        )

        q = q_partition(H, M, Qtot, p)
        HboundQ = q["HboundQ"]
        MboundQ = q["MboundQ"]

        return np.array([
            (H + HboundG + HboundQ) - Htot,
            (M + MboundG + MboundQ) - Mtot
        ])

    lo = np.array([-46.0, -46.0])  # ~1e-20 M
    hi = np.array([  0.0,   0.0])  # 1 M

    sol = least_squares(
        residuals,
        x0=x0_lnHM,
        bounds=(lo, hi),
        xtol=1e-14, ftol=1e-14, gtol=1e-14,
        max_nfev=6000
    )

    lnH, lnM = sol.x
    H = float(np.exp(lnH))
    M = float(np.exp(lnM))
    return sol, H, M


def compute_curve(p: dict):
    """Compute curve; if warning points exist, increase nPts by 1.3x and retry until warnings clear (capped)."""
    nPts_start = int(p["nPts"])
    max_iter = int(p.get("autoRefineMaxIter", 8))
    max_pts = int(p.get("autoRefineMaxPts", 2000))
    grow = float(p.get("autoRefineGrow", 1.3))

    nPts_eff = max(10, nPts_start)
    last = None

    for k in range(max_iter):
        p_local = dict(p)
        p_local["nPts"] = int(nPts_eff)
        last = _compute_curve_once(p_local)
        xs, y, S0, S1, S2, S3, Hfree_mM, Mfree_mM, warn, resid_norm = last
        if (warn is not None) and (not bool(np.any(warn))):
            break
        # grow and retry
        nPts_next = int(math.ceil(nPts_eff * grow))
        if nPts_next <= nPts_eff:
            nPts_next = nPts_eff + 1
        nPts_eff = nPts_next
        if nPts_eff > max_pts:
            break
    # diagnostics
    st.session_state["nPts_used"] = int(nPts_eff) if last is not None else nPts_start
    return last


def _compute_curve_once(p: dict):
    """
    Build titration curve vs equiv Ag0 with dilution.
    Equivalent definition:
      equiv = (moles Ag added) / (initial moles G)
    """
    # initial amounts in mmol
    nG0 = p["G0_mM"] * p["V0_mL"]
    nH0 = p["H0_mM"] * p["V0_mL"]
    nQ0 = p["Q0_mM"] * p["V0_mL"]

    Mtitrant = max(1e-12, p["Mtitrant_mM"])
    xs = np.linspace(0.0, float(p["maxEquiv"]), int(p["nPts"]))

    y = np.zeros_like(xs)
    S0 = np.zeros_like(xs)
    S1 = np.zeros_like(xs)
    S2 = np.zeros_like(xs)
    S3 = np.zeros_like(xs)
    Hfree_mM = np.zeros_like(xs)
    Mfree_mM = np.zeros_like(xs)
    warn = np.zeros_like(xs, dtype=bool)
    resid_norm = np.zeros_like(xs)

    # heuristic initial guess
    H_guess = max((p["H0_mM"] - 2*p["G0_mM"] - (p["Q0_mM"] if p["enable_Q"] else 0.0)) * 1e-3, 1e-15)
    M_guess = 1e-15
    x0 = np.log([H_guess, M_guess])

    for i, eq in enumerate(xs):
        nMadd = eq * nG0   # mmol
        Vadd = nMadd / Mtitrant  # mL
        V = p["V0_mL"] + Vadd

        Gtot = (nG0 / V) * 1e-3  # M
        Htot = (nH0 / V) * 1e-3  # M
        Qtot = (nQ0 / V) * 1e-3  # M
        Mtot = (nMadd / V) * 1e-3  # M

        if i == 0:
            x0 = np.log([max(Htot, 1e-15), max(Mtot, 1e-15)])

        sol, H, M = solve_free_HM(Gtot, Htot, Mtot, Qtot, p, x0)
        x0 = sol.x  # continuation
        resid_norm[i] = float(np.linalg.norm(sol.fun))

        warn[i] = (not sol.success) or (resid_norm[i] > 1e-10)

        c = coeffs_G(H, M, p)
        Gfree = Gtot / sum(c.values())

        S0_i = c["G"] * Gfree
        S1_i = (c["GH"] + c["GHM"] + c["GHM2"]) * Gfree
        S2_i = (c["GH2"] + c["GH2M"] + c["GH2M2"]) * Gfree
        S3_i = (c["GH3"] + c["GH3M"] + c["GH3M2"]) * Gfree

        S0[i] = S0_i * 1e3
        S1[i] = S1_i * 1e3
        S2[i] = S2_i * 1e3
        S3[i] = S3_i * 1e3
        Hfree_mM[i] = H * 1e3
        Mfree_mM[i] = M * 1e3

        if p["yMode"] == "pct":
            y[i] = 100.0 * (S3_i / Gtot)
        elif p["yMode"] == "S3mM":
            y[i] = S3[i]
        elif p["yMode"] == "freeH":
            y[i] = Hfree_mM[i]
        elif p["yMode"] == "freeM":
            y[i] = Mfree_mM[i]
        else:
            y[i] = 100.0 * (S3_i / Gtot)

    return xs, y, S0, S1, S2, S3, Hfree_mM, Mfree_mM, warn, resid_norm

# -----------------------------
# UI
# -----------------------------
st.title("VPIPV Simulator")

with st.sidebar:
    st.header("Initial concentrations (mM)")
    G0_mM = st.number_input("G0 (guest)", value=1.0, step=0.1, format="%.3f")
    H0_mM = st.number_input("H0 (host)", value=3.1, step=0.1, format="%.3f")
    enable_Q = st.selectbox("Enable competitor Q?", ["Yes", "No"], index=0) == "Yes"
    Q0_mM = st.number_input("Q0 (competitor)", value=1.0, step=0.1, format="%.3f", disabled=not enable_Q)

    st.header("Titration / plotting")
    V0_mL = st.number_input("V0 (mL)", value=0.5, step=0.01, format="%.3f")
    Mtitrant_mM = st.number_input("Ag titrant (mM)", value=10.0, step=0.1, format="%.3f")
    xMaxBox = st.number_input("X-axis max (0 to X)", value=3.0, step=0.1, format="%.3f", key="xMaxBox")
    maxEquiv = float(xMaxBox)  # <-- hard override
    nPts = st.number_input("# points", value=61, step=1)

    yMode_label = st.selectbox("Y axis", ["%S", "S [mM]"], index=0)
    showBins = st.selectbox("Show bins S0–S3?", ["No", "Yes"], index=0) == "Yes"

    st.header("Binding constants (log10 K)")
    logKH3M  = st.number_input("log KH3M (GH2M + H ⇌ GH3M)", value=5.0, step=0.1)
    logKH3M2 = st.number_input("log KH3M2 (GH2M2 + H ⇌ GH3M2)", value=5.0, step=0.1)
    logKH1   = st.number_input("log KH1 (G + H ⇌ GH)", value=5.86, step=0.1)
    logKH2   = st.number_input("log KH2 (GH + H ⇌ GH2)", value=5.86, step=0.1)

    allow_KH3 = st.selectbox("Allow GH3 (KH3)?", ["No (KH3=0)", "Yes"], index=0) == "Yes"
    logKH3 = st.number_input("log KH3 (GH2 + H ⇌ GH3)", value=-99.0, step=0.1, disabled=not allow_KH3)

    logKM0  = st.number_input("log KM,0 (G + M ⇌ GM)", value=4.23, step=0.1)
    logKM20 = st.number_input("log KM2,0 (GM + M ⇌ GM2)", value=4.23, step=0.1)
    logKM1  = st.number_input("log KM,1 (GH + M ⇌ GHM)", value=4.23, step=0.1)
    logKM21 = st.number_input("log KM2,1 (GHM + M ⇌ GHM2)", value=4.23, step=0.1)
    logKM2  = st.number_input("log KM,2 (GH2 + M ⇌ GH2M)", value=4.23, step=0.1)
    logKM22 = st.number_input("log KM2,2 (GH2M + M ⇌ GH2M2)", value=4.23, step=0.1)

    st.header("Competitor Q (log10 K)")
    logKQ   = st.number_input("log KQ (Q + H ⇌ QH)", value=4.0, step=0.1, disabled=not enable_Q)
    logKQM  = st.number_input("log KQM (Q + M ⇌ QM)", value=3.0, step=0.1, disabled=not enable_Q)
    logKQHM = st.number_input("log KQHM (QH + M ⇌ QHM)", value=5.0, step=0.1, disabled=not enable_Q)

yMode_map = {"%S":"pct", "S [mM]":"S3mM"}

params = dict(
    G0_mM=float(G0_mM),
    H0_mM=float(H0_mM),
    Q0_mM=float(Q0_mM) if enable_Q else 0.0,
    enable_Q=bool(enable_Q),
    V0_mL=float(V0_mL),
    Mtitrant_mM=float(Mtitrant_mM),
    maxEquiv=float(maxEquiv),
    nPts=int(nPts),
    yMode=yMode_map[yMode_label],
    showBins=bool(showBins),
    logKH3M=float(logKH3M),
    logKH3M2=float(logKH3M2),
    logKH1=float(logKH1),
    logKH2=float(logKH2),
    allow_KH3=bool(allow_KH3),
    logKH3=float(logKH3),
    logKM0=float(logKM0),
    logKM20=float(logKM20),
    logKM1=float(logKM1),
    logKM21=float(logKM21),
    logKM2=float(logKM2),
    logKM22=float(logKM22),
    logKQ=float(logKQ),
    logKQM=float(logKQM),
    logKQHM=float(logKQHM),
)

xs, y, S0, S1, S2, S3, Hfree_mM, Mfree_mM, warn, resid_norm = compute_curve(params)

# --- PLOTTING ---
col1, col2 = st.columns([2.2, 1.0], gap="large")

with col1:
    fig = go.Figure()

    # Plot S0–S3 as percentages
    total = S0 + S1 + S2 + S3
    pct_S0 = np.where(total > 0, 100*S0/total, 0)
    pct_S1 = np.where(total > 0, 100*S1/total, 0)
    pct_S2 = np.where(total > 0, 100*S2/total, 0)
    pct_S3 = np.where(total > 0, 100*S3/total, 0)

    if params["yMode"] == "S3mM":
        # S [mM] mode: plot diluted concentrations (already in mM arrays)
        fig.add_trace(go.Scatter(x=xs, y=S0, mode="lines",
                                 name="S0 (mM)", line=dict(dash="dot")))
        fig.add_trace(go.Scatter(x=xs, y=S1, mode="lines",
                                 name="S1 (mM)", line=dict(dash="dash")))
        fig.add_trace(go.Scatter(x=xs, y=S2, mode="lines",
                                 name="S2 (mM)", line=dict(dash="longdash")))
        fig.add_trace(go.Scatter(x=xs, y=S3, mode="lines",
                                 name="S3 (mM)", line=dict(width=3)))
    else:
        # %S mode
        fig.add_trace(go.Scatter(x=xs, y=pct_S0, mode="lines",
                                 name="S0 (%)", line=dict(dash="dot")))
        fig.add_trace(go.Scatter(x=xs, y=pct_S1, mode="lines",
                                 name="S1 (%)", line=dict(dash="dash")))
        fig.add_trace(go.Scatter(x=xs, y=pct_S2, mode="lines",
                                 name="S2 (%)", line=dict(dash="longdash")))
        fig.add_trace(go.Scatter(x=xs, y=pct_S3, mode="lines",
                                 name="S3 (%)", line=dict(width=3)))

    if np.any(warn):
        fig.add_trace(go.Scatter(
            x=xs[warn], y=(S3[warn] if params["yMode"]=="S3mM" else pct_S3[warn]),
            mode="markers", marker=dict(symbol="x", size=10),
            name="solver warning points"
        ))

    fig.update_layout(
        height=740,
        margin=dict(l=40, r=20, t=40, b=40),
        xaxis=dict(title="equiv Ag0", range=[0, float(maxEquiv)], autorange=False),
        yaxis=(dict(title="Population (%)", range=[0, 100]) if params["yMode"]=="pct" else dict(title="S [mM]", range=[0, float(params["G0_mM"]) ])),
        template="plotly_dark",
        uirevision=f"xmax={float(maxEquiv):.6f}",
        legend=dict(x=0.01, y=0.99),
    )

    st.plotly_chart(fig, use_container_width=True, key=f"chart_xmax_{float(maxEquiv):.6f}")

with col2:
    st.subheader("Quick readouts")
    st.write(f"**Solver warning points:** {int(np.sum(warn))} / {len(xs)}")

    imax = int(np.argmax(pct_S3))
    st.write(f"**Peak S3:** {pct_S3[imax]:.2f}% at equiv **{xs[imax]:.3g}**")
    st.write(f"**Final S3:** {pct_S3[-1]:.2f}% at equiv **{xs[-1]:.3g}**")

    st.subheader("Free species (last point)")
    st.write(f"free [H] ≈ **{Hfree_mM[-1]:.4g} mM**")
    st.write(f"free [M] ≈ **{Mfree_mM[-1]:.4g} mM**")

    st.subheader("Bins at last point (mM)")
    st.write(f"S0 = {S0[-1]:.4g}")
    st.write(f"S1 = {S1[-1]:.4g}")
    st.write(f"S2 = {S2[-1]:.4g}")
    st.write(f"S3 = {S3[-1]:.4g}")
