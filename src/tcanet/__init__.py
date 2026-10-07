"""TCANet — task-driven cross-layer agentic networking (paper modules).

Package map (paper section -> module):

* ``spec``       — ``T_m``, DAG, ``q^H/q^S``, gateway graph, world (Sec. II-A)
* ``binding``    — supporting-agent bindings ``Phi_m`` (Sec. II-A)
* ``subnet``     — versioned subnet state ``S_m^(v_m)`` + construction (Sec. IV-A)
* ``candidates`` — per-layer authorized candidates ``U^l_m`` (Sec. II-B, III-A)
* ``feasibility``— projected state + joint feasibility, Eq. 5-8 (Sec. III-A)
* ``selection``  — two-stage selection: min J_m, then min M_m (Eq. 9-12)
* ``closure``    — dependency-scope expansion over ``D_m`` (Eq. 13-16)
* ``executor``   — staged execution of the selected decision (Sec. IV-B)
* ``verify``     — Assess / Rollback / retry up to ``K_max`` (Sec. IV-C, Alg. 1)
* ``metrics``    — formation/recovery latency, ``Mod_m`` (Sec. V-A)
* ``scenario``   — paper Fig. 1 rescue scenario and runtime events
* ``demo``       — CLI narrative and web-trace generation for demos
"""
