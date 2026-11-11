# PRD — AMLens

**AI copilot for anti-money-laundering investigations**

**Version:** v1.1 (final) · **Date:** 5 Nov 2026 · **Owner:** Sparsh · **Repo:** `amlens` · **Status:** LOCKED — no more pivots
**Bootcamp mapping:** FDE Bootcamp Capstone 1 (OmniGuard: secure integration) + Capstone 2 (AuditMesh: multi-agent compliance), applied to anti-money-laundering investigations

---

## 1. One-line summary
**AMLens** is a multi-agent copilot that investigates anti-money-laundering (AML) alerts the way a bank analyst does — querying transactions, tracing money flows, matching known laundering patterns, and drafting a cited case narrative — with a compliance officer approving every decision.

## 2. Problem
Banks run transaction-monitoring rules that fire alerts on suspicious activity. Analysts must investigate every alert by hand.
- Rule-based monitoring commonly produces false-positive rates above 90% (Guidehouse analysis, 2026)
- In a March 2026 survey, 53% of banks spent an hour or more per alert, a quarter four hours or more (Liminal)
- Regulators still require a human to sign off every suspicious activity report (SAR)

**The manual 5-step investigation (to map and validate in discovery):**
1. **Triage** the alert: what fired, how risky is this customer?
2. **Gather data:** customer profile, account history, related transactions
3. **Trace the money:** counterparties, fan-in/fan-out, cycles, layering
4. **Match a typology:** which known laundering pattern does this look like?
5. **Decide and document:** close as false positive, or escalate with a written SAR narrative

Steps 2–5 are where the time goes. The copilot automates the gathering, tracing, matching and drafting. **Humans keep the decision.**

## 3. Users and roles (RBAC)
| Role | Does | Can see / do |
|---|---|---|
| **L1 Analyst** | Reviews alerts, accepts/edits agent findings, closes false positives | Alerts, customer data (PII masked), investigation tools |
| **L2 Investigator** | Handles escalated cases, edits SAR drafts | Everything L1 + full PII + case history |
| **Compliance Officer (MLRO)** | **Approves or rejects SAR filings** | Everything + approval rights + audit log |
| **Model Risk / Admin** | Monitors model and agent quality, thresholds, users | Metrics, evals, config, audit log (no case decisions) |

## 4. Goals and non-goals
**Goals**
- Cut analyst time per alert while **not missing** true laundering (recall is the safety metric)
- Every agent claim is **evidence-backed**: it cites transaction IDs or retrieved guidance
- Every case decision and SAR is human-approved and fully auditable
- Measured against baselines, enforced by a CI eval gate

**Non-goals:** real bank data, real SAR filing to regulators, KYC onboarding, sanctions/PEP screening (stretch), a customer-facing product, autonomous decisions.

## 5. Data plan (no outside dependencies)
| Data | Source | Real / synthetic |
|---|---|---|
| Transactions + accounts with laundering labels | **IBM AMLworld, HI-Small** (Kaggle: `ealtman2019/ibm-transactions-for-anti-money-laundering-aml`) — ~5M transactions, ~1 in 980 laundering. Check the patterns file for per-attempt typology labels | Synthetic by IBM Research (NeurIPS 2023), complete ground truth |
| Backup dataset | SAML-D (Kaggle): ~9.5M transactions, 28 labeled typologies | Synthetic, labeled |
| Typology guidance for RAG | Public FATF and FIU typology reports, red-flag indicators | ✅ Real (public) |
| Internal policies | Investigation SOP, escalation thresholds, SAR template — written by you | 🔧 Synthetic, labeled |
| Customer profiles (KYC) | Generator: name, occupation, expected activity, risk rating, linked to IBM accounts | 🔧 Synthetic |
| Messy human layer | Entity-name variants ("Acme Ltd" / "ACME Limited"), duplicate records, analyst notes | 🔧 Synthetic |
| Golden set | ~300 alerts hand-reviewed by you (disposition + typology + key evidence) | 🔧 → reviewed |

Large data files are gitignored; a script downloads and loads them reproducibly.

## 6. Architecture
```
Transactions (CSV → MS SQL "core banking warehouse", read-only)
   │
   ├─► Monitoring rules engine ──► Alerts (Postgres)            [deterministic, deliberately noisy]
   │                                  │
   │                          XGBoost risk scorer (MLflow)       [classical ML baseline + prioritisation]
   │                                  │
   │                     LangGraph SUPERVISOR (investigation)
   │        ┌────────────┬────────────┼─────────────┬───────────────┐
   │   Data agent    Graph agent   Typology agent  Narrative agent  QA / critic agent
   │   (text-to-SQL) (networkx)    (hybrid RAG)    (cited draft)    (checks every claim)
   │                                  │
   │              Human approval (L1 → L2 → Compliance Officer)
   │                                  │
   └──────────── Case management via custom MCP server (tickets, notes, status) + audit log
```
- **FastAPI modular monolith**, Python 3.12, async SQLAlchemy + asyncpg, Alembic, Pydantic v2
- **Postgres 16 + pgvector:** alerts, cases, RAG index, job queue (`FOR UPDATE SKIP LOCKED`), LangGraph checkpoints
- **MS SQL (Docker):** the bank's transaction warehouse — read-only, text-to-SQL target
- **ML:** scikit-learn + XGBoost, MLflow tracking + model registry
- **Agents:** LangGraph supervisor; **deep-agent mode** (planning, sub-agents, scratchpad) for complex alerts
- **MCP:** custom MCP server exposing case-management tools with explicit trust boundaries
- **Guardrails:** Presidio (PII masking by role), NeMo Guardrails (topic + injection rails), tool allowlists per role
- **Observability:** structlog + Langfuse (traces, token cost per alert, latency per agent)
- **UI:** Streamlit approval workbench — thin client over the API only
- **Deploy:** Docker Compose → AWS (ECS or EKS); Kafka streaming and Kubernetes as stretch

## 7. Features
| ID | Feature | Phase |
|---|---|---|
| F1 | Data loader: IBM CSVs → MS SQL; synthetic KYC + policies | MVP |
| F2 | Monitoring rules engine (structuring, rapid movement, high-risk corridors, fan-in/out) → alerts | MVP |
| F3 | Baselines: rules-only, XGBoost risk scorer on graph + behavioural features (MLflow) | MVP |
| F4 | **Secure text-to-SQL** tool over MS SQL: SELECT-only, sqlglot validation, table/column allowlists per role, forced LIMIT, timeout, read-only user | MVP |
| F5 | Graph tool: counterparty network, cycles, fan-in/fan-out, hop distance — networkx from scratch first, then **Neo4j + Cypher** with a guarded text-to-Cypher tool (SQL for relational questions, graph for multi-hop money flows) | MVP |
| F6 | **Hybrid RAG** over typologies + SOPs (pgvector + full-text, fusion, reranker), permission-aware | MVP |
| F7 | Investigation agent: raw tool loop first, then LangGraph supervisor + sub-agents | MVP |
| F8 | Cited SAR narrative drafter + QA critic (every claim must reference transaction IDs or a source chunk) | MVP |
| F9 | Human-approval workflow: L1 → L2 → Compliance Officer, LangGraph interrupts + checkpoints, outbox for actions | MVP |
| F10 | Custom MCP server for case tickets (create, update, add note, change status) with least-privilege scopes | MVP |
| F11 | Auth + RBAC (JWT, mock OAuth 2.0), PII masking by role, append-only audit log | MVP |
| F12 | Guardrails: Presidio, NeMo, prompt-injection suite (malicious text planted in memo/notes fields) | MVP |
| F13 | Observability + cost/latency dashboard; SLAs per alert; **LiteLLM AI gateway** in front of all model calls (routing, fallbacks, budgets per role, rate limits) | MVP |
| F14 | Eval harness + CI gate (see §9) | MVP |
| F15 | Deep-agent mode for complex alerts + routing by complexity; measured vs LangGraph flow | Stretch-1 |
| F16 | ML lifecycle: fine-tuned DistilBERT pre-classifier (LoRA), distilled from LLM labels, INT8 quantization, drift check + retrain | Stretch-1 |
| F17 | Kafka/Redpanda streaming → near-real-time alerting + load test | Stretch-2 |
| F18 | Kubernetes deploy | Stretch-2 |

**Cut order if behind:** F18 → F17 → F16 → F15. Evals (F14) and guardrails (F11–F12) are never cut.

## 8. Consulting deliverables (Pillar 1 — both capstones)
| # | Deliverable | Capstone |
|---|---|---|
| 01 | **Discovery:** 2–3 conversations with AML/compliance professionals (LinkedIn outreach) + findings | 1 |
| 02 | **Workflow map:** the 5-step manual investigation, time per step, human-in-the-loop bottlenecks | 2 |
| 03 | **Data classification:** public / internal / confidential / PII; what each role may see | 1 |
| 04 | **Architecture doc + SOW:** scope, milestones, acceptance criteria | 1 |
| 05 | **SLAs:** latency and cost per alert, accuracy floors | 2 |
| 06 | **MCP trust boundaries:** which agent can call which tool, with what scope | 2 |
| 07 | **Build-vs-buy** vs vendors (Lucinity, Hawk, Unit21, WorkFusion) | — |
| 08 | **ROI + CISO/compliance briefing deck** | 1 |
| 09 | **UAT runbook** | 1 |
| 10 | **Operations + training handoff:** how analysts use it, how model risk monitors it | 2 |

## 9. Success metrics (starting targets — revise after the first baseline run)
| Metric | Baselines | Target |
|---|---|---|
| Alert disposition **recall** on true laundering (safety) | Rules-only, XGBoost | ≥ 0.95 |
| **False-positive reduction** at that recall | Rules-only | ≥ 50% |
| PR-AUC of risk scoring | Rules-only | Report |
| Typology identification accuracy (golden set) | — | ≥ 0.80 |
| Narrative faithfulness: claims backed by cited evidence | — | ≥ 0.95 |
| Text-to-SQL execution accuracy (100 questions) | — | ≥ 0.85 |
| Retrieval recall@5 (150 labeled questions) | Vector-only | ≥ 0.90 |
| Prompt-injection success rate | — | 0% |
| RBAC/PII leak tests | — | 0 leaks |
| Cost per alert / p95 latency | — | Tracked; SLA set in deliverable 05 |
| LangGraph flow vs deep agent (accuracy, cost, latency) | — | Reported (F15) |

Never tune on the test split. Report synthetic results honestly as synthetic.

## 10. Timeline — 4 Nov 2026 → 17 Jan 2027
Each week: Track B pieces built **from scratch first, then with the library**. The "learn" column is the concept to study that week (from the AI/ML course).

| Week | Dates | Build | Learn | Milestone |
|---|---|---|---|---|
| 0 | 4–8 Nov | Repo bootstrap, download IBM data, explore in a notebook, load into MS SQL, discovery outreach | Data exploration, class imbalance | Data loaded · outreach sent |
| 1 | 9–15 Nov | Rules engine → alerts; rules-only baseline; XGBoost + MLflow; eval harness v0 | Train/val/test, precision/recall, PR-AUC, gradient boosting | **Baseline report** |
| 2 | 16–22 Nov | Secure text-to-SQL (MS SQL); graph tool; typology RAG + retrieval evals | Embeddings, hybrid search, rerankers | Tools measured |
| 3 | 23–29 Nov | Raw agent loop → LangGraph single agent; narrative drafter; approval UI | Tool calling, ReAct, state machines | **End-to-end demo** |
| 4 | 30 Nov–6 Dec | Multi-agent supervisor + QA critic; MCP case server; checkpoints | Multi-agent patterns, MCP | Multi-agent working |
| 5 | 7–13 Dec | JWT/RBAC, PII masking, Presidio, NeMo, injection suite, audit log; Langfuse cost dashboard; CI eval gate | Prompt injection, guardrails, LLMOps | **Demo-ready → start Big 4 applications + referrals** |
| 6 | 14–20 Dec | Deep-agent mode + routing by complexity (F15) | Deep agents | Comparison report |
| 7 | 21–27 Dec | DistilBERT + LoRA, distillation, INT8, drift/retrain (F16) | Neural nets, fine-tuning, LoRA, quantization | ML lifecycle done |
| 8 | 28 Dec–3 Jan | Kafka streaming + load test (F17) | Streaming basics | Load numbers |
| 9 | 4–10 Jan | AWS deploy (+ Kubernetes stretch); consulting docs finalised | Cloud deployment | Live URL |
| 10 | 11–17 Jan | README with metrics + decision log, 2-min demo video, case-study write-up, resume | — | **🚀 Ready to apply (18 Jan)** |

**Daily rhythm:** build 4–5 h · DSA + SQL 45 min · AWS 1 h. When BachatAI restarts (mid-Nov), BachatAI work first; this project continues at reduced pace, cutting stretch items before MVP items.
**Weekly review (Sunday):** milestone hit? cost spent? cut anything?

## 11. Interview story (fill numbers as they come)

**Why I built it (30 seconds):**
> "At my fintech startup I work with LLMs that extract transactions from users' bank emails, so I've seen how messy financial data is — and that in finance an AI mistake means wrong money, not just a wrong answer. I wanted to go deep into production AI in **regulated** finance, so I picked a problem that forces the hard parts: extreme class imbalance, measurable accuracy, auditable claims, strict access control, human approval and millions of rows. AML alert investigation has all of them: most alerts are false positives and analysts spend hours on each. I built AMLens and measured it on IBM's AML benchmark, where every laundering transaction is labeled."

**What it does and how I know it works (2 minutes):**
> "AMLens queries the transaction warehouse through guarded text-to-SQL, traces money flows in Neo4j, matches typologies with cited guidance, and drafts a case narrative a compliance officer must approve. Against a rules-only baseline it cut false positives by __% at __ recall; __% of narrative claims are evidence-linked; cost is __ per alert. The hardest part was __."

**Follow-ups:**
| Question | Answer |
|---|---|
| Why synthetic data? | Real bank data is private by law. IBM's benchmark is realistic and has *complete* labels — real data doesn't, because most laundering is never caught |
| Is anyone using it? | It's a portfolio project, not deployed at a bank — but run like a client engagement (workflow map, SOW, SLAs, UAT). *Only if true:* "validated with __ compliance professionals" |
| Why agents, not just an ML model? | The model scores alerts; the agents investigate and explain them with evidence. Analysts need the why. I measured both |

**Resume bullets (draft):**
- Built **AMLens**, a multi-agent AML investigation copilot (LangGraph supervisor, guarded text-to-SQL on MS SQL, graph analysis, hybrid RAG, custom MCP case server) with mandatory compliance-officer approval
- Reduced false positives by __% at __ recall vs a rules-only baseline on __M transactions; XGBoost baseline tracked in MLflow; CI eval gate on every PR
- Enforced RBAC with role-based PII masking (Presidio), NeMo guardrails and an injection suite (0% success); traced cost and latency per alert with Langfuse

## 12. Risks
| Risk | Mitigation |
|---|---|
| BachatAI takes most of the day | Cut order in §7; MVP by 13 Dec is the priority |
| Synthetic data "too clean" | Messy human layer, adversarial notes, report honestly |
| Agent costs | Cheap model default, caching, small CI subsets, spending cap ₹8,000 total |
| Scope creep / pivot urge | This PRD is locked. New ideas go to `docs/ideas.md`, reviewed after 18 Jan |
| "AI built this for you" suspicion | Track B written by hand; frequent commits; decision log; dated eval reports; error analysis; discovery calls; build-in-public posts |

## 13. Proof of real work (portfolio evidence)
- **Commit history** over 10 weeks with small, clear commits
- **Decision log** (`docs/decisions/`): what was tried, what failed, why it changed
- **Eval history**: baseline → v1 → v2, with what changed each time
- **Error analysis**: real failure examples and fixes per component
- **Discovery**: conversations with AML/compliance professionals; demo feedback from them
- **Build in public**: a LinkedIn post every 1–2 weeks on what was built and what broke
- **Honest README**: architecture, metrics next to baselines, synthetic-data disclaimer, limitations, 2-minute demo video
