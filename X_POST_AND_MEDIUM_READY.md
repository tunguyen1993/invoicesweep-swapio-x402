# InvoiceSweep x402 × Swap.io — Ready-to-Post X Video Caption & Medium Article

---

## 1. Ready-to-Post X (Twitter) Video Caption

> Copy and paste directly when uploading the 60–120s pitch video (`swapio_x402_superteam_1080p.mp4`):

Solana freelancers & micro-DAOs get paid 5–10x/month in volatile SPL tokens ($SOL, $JUP, $BONK, $PYTH) — so why burn $99/mo on idle DEX API keys just to auto-settle invoices into USDC?

Meet **InvoiceSweep x402** powered by @swapdotio & @Superteam ⚡

1️⃣ Agent watches incoming client invoices
2️⃣ Calls `hermes.swap.io/v1/swap/quote` -> gets `HTTP 402 Payment Required`
3️⃣ Pays fractions of a cent in gasless USDC via x402 -> gets ranked Routhex quotes + builds the unsigned swap tx (`/v1/swap/build-tx`) with on-chain `X-PAYMENT-RESPONSE` proof

8 swaps/mo = pennies instead of a $99/mo subscription. Zero API keys. Zero `.env` secret leaks. Zero charge when an invoice is already in USDC.

Live Demo & Video: https://tunguyen1993.github.io/invoicesweep-swapio-x402/
Full Article: https://tunguyen1993.github.io/invoicesweep-swapio-x402/article.html
Try Swap.io: https://swap.io

---

## 2. Ready-to-Publish Medium Article

### Title
**InvoiceSweep x402: Why Autonomous Solana Bookkeeping Agents Pay Per Quote on Swap.io Instead of Buying $99/mo API Keys**

### Subtitle
*How HTTP 402 micro-settlements on Swap.io’s Hermes API turn sporadic multi-token freelancer and Micro-DAO invoices into clean USDC — with zero API keys, zero SOL gas for payments, and 99.9%+ lower infrastructure overhead.*

---

If you work as an independent smart-contract auditor, designer, devshop, or Micro-DAO contributor on Solana, your revenue rarely arrives in a single asset. In a typical month, you might receive **5 to 10 invoices** across ecosystem grants, retainers, and bounties denominated in **SOL, JUP, BONK, and PYTH** — while your rent, payroll, and tax liabilities are strictly in **USDC**.

Building an autonomous AI bookkeeping agent (**InvoiceSweep x402**) to watch your receiving wallet and immediately convert volatile SPL payouts into USDC sounds simple on paper. In practice, traditional Web3 API billing breaks the economics of self-hosted agents before you write line one of swap logic.

Today, [**Swap.io**](https://swap.io) solves this natively through its **x402 Agent API (`https://hermes.swap.io`)** — letting any autonomous agent buy multi-provider swap quotes and unsigned transactions **per request** over standard `HTTP 402 Payment Required`.

*(Embed your 60–120s X video or YouTube/Vimeo mirror right here in the Medium editor before hitting Publish.)*

---

### 1. The Concrete Use Case: InvoiceSweep x402

**InvoiceSweep x402** is a lightweight, self-hosted treasury sweeper built for Solana freelancers and small contributor guilds:

1. **Watch Incoming Transfers:** The agent monitors a designated invoice address for incoming SPL token transfers against open client invoice IDs.
2. **Skip USDC Invoices Automatically ($0 API Spend):** Whenever a client pays directly in `USDC` (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`), InvoiceSweep marks the invoice settled and makes **zero** external API calls.
3. **Quote & Settle Volatile SPL Tokens via Swap.io:** When a non-USDC token (`SOL`, `JUP`, `BONK`, `PYTH`) lands, InvoiceSweep queries [Swap.io](https://swap.io)’s Hermes routing engine (`routhex`), selects the highest-output route across Jupiter, DFlow, and OKX DEX, builds an unsigned Solana swap transaction, verifies strict `Decimal` slippage bounds, and hands the unsigned transaction to the local treasury signer.

---

### 2. How the Swap.io `x402` Flow Works Under the Hood

Instead of registering developer accounts, provisioning OAuth apps, or storing static `API_KEY` secrets in `.env` files, Swap.io prices every programmatic call natively in HTTP using the **x402 protocol**:

#### Step 1 — Unpaid Request Returns `HTTP 402 Payment Required`
When InvoiceSweep sends an unauthenticated `POST` to `https://hermes.swap.io/v1/swap/quote`, Hermes responds immediately with `HTTP 402` and machine-readable payment terms (`x402Version: 1`):

```json
{
  "x402Version": 1,
  "accepts": [
    {
      "scheme": "exact",
      "network": "solana",
      "maxAmountRequired": "3007",
      "resource": "https://hermes.swap.io/v1/swap/quote",
      "description": "Paid swap API (routhex): quote and build unsigned transactions; caller signs and broadcasts",
      "mimeType": "application/json",
      "payTo": "BPz2u6eoWLervxEqfpsMCVmKk7h6fEVsd7L5rvEibMfD",
      "maxTimeoutSeconds": 60,
      "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
      "extra": {
        "feePayer": "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"
      }
    }
  ]
}
```

Notice three critical engineering details from this live `hermes.swap.io` capture:
- **6-Decimal Atomic USDC Pricing:** `maxAmountRequired` is denominated in micro-USDC (`"1000"` = `$0.001 USDC`; parameter-aware live quote calls price around `"3000"`–`"3007"` = `~$0.003 USDC`). InvoiceSweep enforces a hard `Decimal` per-call ceiling before approving any payment.
- **Gasless API Payments (`extra.feePayer`):** Swap.io’s facilitator sponsors the Solana network fee for the x402 payment itself. A dedicated agent API wallet only needs a couple dollars of **USDC** and **zero SOL**.
- **Zero Charge on Discovery or Upstream Errors:** Unpaid `402` probes and any upstream `4xx/5xx` errors never settle payment.

#### Step 2 — Pay in USDC & Receive Ranked Multi-DEX Quotes
Using an x402 client (`x402-solana` or `@x402/fetch` + `@x402/svm`), the agent signs the exact micro-USDC payment and retries `POST /v1/swap/quote` with the `X-PAYMENT` header. Hermes returns `HTTP 200 OK` with ranked quotes (`quotes[0]` is `bestQuote`) plus a base64 `X-PAYMENT-RESPONSE` header containing the on-chain settlement signature.

#### Step 3 — Build the Unsigned Swap Transaction (`/v1/swap/build-tx`)
InvoiceSweep passes the winning route’s raw `providerQuote` object (`quoteResponse: best.providerQuote` — never the outer wrapper) to `POST https://hermes.swap.io/v1/swap/build-tx`. A second sub-cent x402 settlement returns the unsigned base64 Solana `VersionedTransaction` plus its own `X-PAYMENT-RESPONSE` receipt, ready for local signing and RPC broadcast.

---

### 3. Why `x402` Beats API Keys & SaaS Subscriptions for AI Agents

| Dimension | Traditional DEX API Subscriptions | Swap.io Agent API (`x402`) |
| :--- | :--- | :--- |
| **Onboarding & KYC** | Email signup, billing portal, credit card, tier quotas | **Zero signup** — any agent with USDC works on boot |
| **Secret Hygiene** | Long-lived `API_KEY` in `.env` vulnerable to repo leaks | **Zero static secrets** — per-call cryptographic authorization |
| **Idle Month Cost** | **$99.00 / month** burned even if 0 invoices arrive | **$0.000000 / month** when idle |
| **8 Invoices / Month** | **$99.00 / month** (`$12.37` API overhead per swap!) | **`$0.016` to `$0.048 / month`** (**99.95%–99.98% savings**) |
| **Failure Billing** | Quota consumed on failed calls or rate-limit retries | **Failed `4xx/5xx` requests are never charged** |

At 8 non-USDC invoices per month, a freelancer or Micro-DAO saves **over $1,187 per year** per self-hosted agent — and you would need tens of thousands of swaps every single month before a flat $99/mo subscription ever broke even against [Swap.io](https://swap.io).

---

### Explore Swap.io & x402

- **Swap.io Platform:** [https://swap.io](https://swap.io)
- **Live InvoiceSweep x402 Interactive Demo & 84s HD Video:** [https://tunguyen1993.github.io/invoicesweep-swapio-x402/](https://tunguyen1993.github.io/invoicesweep-swapio-x402/)
- **Hosted Editorial Article (with Embedded 1080p Video):** [https://tunguyen1993.github.io/invoicesweep-swapio-x402/article.html](https://tunguyen1993.github.io/invoicesweep-swapio-x402/article.html)
- **Open-Source Reference Agent & Tests:** [https://github.com/tunguyen1993/invoicesweep-swapio-x402](https://github.com/tunguyen1993/invoicesweep-swapio-x402)
- **Swap.io Hermes x402 API Discovery:** [https://hermes.swap.io/v1/info](https://hermes.swap.io/v1/info)
- **Swap.io Agent API Quickstart:** [https://docs.swap.io/developer-tools/agent-api-x402/quickstart](https://docs.swap.io/developer-tools/agent-api-x402/quickstart)
- **Follow on X:** [@swapdotio](https://x.com/swapdotio) & [@Superteam](https://x.com/Superteam)
