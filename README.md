**Important:** This is a sample project for demonstration purposes only. It is not intended for production use without thorough review, testing, and hardening.

![logo](/media/logo.png)

# Support Breakdown Report

*Read this in another language: [Português](README.pt-BR.md).*

Generate a per-linked-account CSV of AWS Enterprise Support charges. The report
can cover a single billing month or a look-back window of several months.

## How it works

The tool sources all of its data from three AWS Billing APIs and combines them
into one CSV row per linked account, per billing month:

| API | Provides |
|-----|----------|
| `get-enterprise-support-contract-details` | Pricing plan, allocation method, and which accounts are charged (and at what percentage). |
| `get-enterprise-support-charge-summary` | Month-level totals: total Support-eligible spend and total Support charge. |
| `list-enterprise-support-linked-account-charges` | Per-linked-account breakdown: eligible spend, prorated spend, billable seconds, and link/subscription periods (paginated). |

## Requirements

- Python 3.10+
- `boto3 >= 1.43.0` — the Enterprise Support billing APIs are only available in
  the 1.43.x line and later (they are missing in 1.42.x). The script detects an
  older SDK and tells you to upgrade instead of failing obscurely.
- AWS credentials with permission to call the Enterprise Support billing APIs.

boto3 is pre-installed in AWS CloudShell, so no setup is needed there.

## Setup

### AWS CloudShell

boto3 is already available. Just make sure it is recent enough:

```bash
pip install --upgrade boto3
```

### Local machine

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configure credentials with a named profile, environment variables, or any
method supported by the AWS SDK credential chain.

## Usage

Use the interactive console. `console.py` is a small REPL; the `run` command
generates the report, forwarding any options to it.

```bash
python console.py
```

```
support-breakdown> run
support-breakdown> run --lookback 3
support-breakdown> run --billing-month 2026-07 --output report.csv
support-breakdown> help
support-breakdown> quit
```

### Options

Pass these to the `run` command:

| Option | Default | Description |
|--------|---------|-------------|
| `--lookback N` | `1` | Number of billing months to include, counting back from the end month. |
| `--end-month YYYY-MM` | last complete month | Most recent billing month to include. |
| `--billing-month YYYY-MM` | – | Report a single month. Overrides `--lookback`/`--end-month`. |
| `--output PATH`, `-o PATH` | `support-breakdown-report-<timestamp>.csv` | Output CSV path, or `-` for stdout. When omitted, a timestamp suffix is added so runs do not overwrite each other. |
| `--profile NAME` | environment/default | AWS named profile to use. |

## Output format

The CSV uses CRLF line endings. Rows are grouped by payer account and sorted
ascending by account id within each group.

### Columns

| Column | API source | Official field definition |
|--------|------------|---------------------------|
| `description` | contract `pricingPlans[0].description` | Pricing plan description for the customer's plan in that billing month (e.g. `Partner Led Pricing Plan 2.0`, `Public Pricing Plan`). Since the CuPPA migration this field may not be fully consistent. |
| `charge_allocation_method` | contract `supportAllocationMethod` | Charge method, verbatim from the API: `Proportional` or `Fixed_Percentage` (see below). |
| `charge_account_id` | linked account `payerAccountId` | The account charges are applied to. For ES / Unified Operations profiles this is the payer account on the customer's profile. |
| `total_aws_charges` | summary `totalSupportEligibleSpend` | Aggregated AWS spend used to derive the support charge. Equals the sum of `account_prorated_charges + account_ri_charges + account_sp_charges`. Same on every row. |
| `support_charges` | per-account charge summed per payer account. Per account: Fixed_Percentage → summary `totalSupportCharge` × contract `chargedPayerAccountIds[].chargePercentage`; Proportional → summary `totalSupportCharge` × (`account_prorated_charges` ÷ Σ `account_prorated_charges`). | Total amount charged to this row's **payer account** (the sum of the charges of every account under that payer). Same on every row of the same payer. `0` means the payer was not charged this period. |
| `total_support_charges` | summary `totalSupportCharge` | Total support charge for the entire billing profile, before allocation. Same on every row. |
| `support_charge_percentage` | derived: `support_charges ÷ total_support_charges` | Fraction of the total support charge billed to this row's **payer account** (`0` when the total is `0`). Same on every row of the same payer. |
| `account_id` | linked account `accountId` | The linked (or payer) account tracked under the billing profile. |
| `payer_account_id` | linked account `payerAccountId` | The payer account the account id is linked to. |
| `account_total_charges` | linked account `totalSupportEligibleSpend` (4 dp) | Total AWS spend used to calculate the support charge, before proration. |
| `account_prorated_charges` | linked account `proratedTotalSupportEligibleSpend` | `account_total_charges × (account_billable_seconds ÷ account_total_seconds)`. |
| `account_total_seconds` | linked account `totalSeconds` | Total seconds in the billing month. |
| `account_billable_seconds` | linked account `billableSeconds` | Billable seconds based on the account's subscription period. |
| `account_ri_charges` | linked account `totalSupportEligibleReservedInstanceSpend` | Reserved Instance purchase charges. |
| `account_sp_charges` | linked account `totalSupportEligibleSavingsPlanSpend` | Savings Plan purchase charges. |
| `account_link_periods` | linked account `linkedTimePeriods` (formatted) | Date/time period the account was linked to the payer account. |
| `account_subscription_periods` | linked account `subscriptionTimePeriods` (formatted) | Date/time period the account was subscribed to the support product. |
| `bill_month` | reporting month | Billing period as `YYYY-MM` (e.g. `2026-07`). |

### Charge allocation methods

The `charge_allocation_method` column is written verbatim from the API's
`supportAllocationMethod` field, which has two valid values:
`Proportional` and `Fixed_Percentage`. There are two charge behaviours:

- **Fixed_Percentage** — support charges are distributed across payer accounts
  according to pre-configured percentages from the contract
  (`chargedPayerAccountIds[].chargePercentage`). The charge for each charged
  account is `total_support_charge × chargePercentage`; every other account is
  `0`.
- **Proportional** — support charges are distributed to each account in
  proportion to its prorated eligible spend. In this mode every
  `chargedPayerAccountIds[].chargePercentage` is `0.0`, so the per-account
  amount cannot come from the contract and is computed as
  `total_support_charge × (account_prorated_charges ÷ Σ account_prorated_charges)`.

> **Implementation note:** the mode is treated as **Proportional** when either
> signal indicates it: the contract's `supportAllocationMethod` is
> `Proportional`, or the contract has charged accounts and **all** of their
> `chargePercentage` values are `0.0`. In that case the report allocates the
> total support charge by each account's share of the prorated eligible spend.
> Otherwise (`Fixed_Percentage`) it uses the contract percentages directly, and
> when there are no charged accounts at all, per-account charges stay `0`.

## Troubleshooting

- **"This boto3/botocore version does not support the Enterprise Support billing
  APIs"** — upgrade the SDK: `pip install --upgrade boto3 botocore`. Make sure
  you upgrade the same interpreter/virtualenv you run the script with.
- **"Failed to initialize the AWS billing client"** — usually an unknown
  `--profile` or missing credentials. Check your AWS configuration.
- **`WARN: no linked account charges returned for <month>`** — the APIs returned
  no data for that month (for example, a month with no active contract). Other
  months in the range are still processed.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.