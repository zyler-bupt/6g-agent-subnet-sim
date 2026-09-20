"""TCANet — task-driven cross-layer agentic networking (paper modules).

Package map (paper section -> module):

* ``spec``       — ``T_m``, DAG, ``q^H/q^S``, gateway graph, world (Sec. II-A)
* ``binding``    — supporting-agent bindings ``Phi_m`` (Sec. II-A)
* ``subnet``     — versioned subnet state ``S_m^(v_m)`` + construction (Sec. IV-A)
* ``candidates`` — per-layer authorized candidates ``U^l_m`` (Sec. II-B, III-A)
* ``feasibility``— projected state ``x_hat_m(u)`` + Eq. 3 checks (Sec. III-A)
* ``selection``  — two-stage lexicographic selection (Eq. 4-7)
* ``closure``    — affected-set closure over ``D^res/D^cfg`` (Eq. 8)
* ``executor``   — staged execution of the selected decision (Sec. IV-B)
* ``verify``     — verification window ``W_m`` and ``B_r`` recovery (Sec. IV-C)
* ``metrics``    — formation/recovery latency, ``Mod_m`` (Eq. 10)
* ``scenario``   — paper Fig. 1 rescue scenario and runtime events
* ``demo``       — CLI narrative and web-trace generation for demos
"""
