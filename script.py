#!/usr/bin/env python3
"""Generate a Support Breakdown Report (CSV).

This tool builds a per-linked-account CSV report. It sources all of its
data from three AWS Billing APIs:

  * get-enterprise-support-contract-details
      Contract-level metadata: pricing plan, allocation method, and which
      payer accounts are charged (and at what percentage).
  * get-enterprise-support-charge-summary
      Month-level totals: total Support-eligible spend and the total Support
      charge.
  * list-enterprise-support-linked-account-charges
      Per-linked-account breakdown: eligible spend, prorated spend, billable
      seconds, and the link/subscription time periods.

The report can span multiple billing months via a look-back period
(``--lookback``).

Usage examples:

  # Last complete month only, written to a timestamped
  # support-breakdown-report-<timestamp>.csv file
  python script.py

  # Last 3 complete months
  python script.py --lookback 3

  # A specific month, to stdout
  python script.py --billing-month 2026-07 --output -
"""

import argparse
import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

# boto3 is only needed at runtime to call the billing APIs. We guard the import
# so the module can still be imported (e.g. for its pure helper functions or by
# the interactive console) on machines where boto3 is not installed. In that
# case main() exits early with a clear message.
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None

    # Lightweight stand-ins so the ``except`` clauses in main() remain valid
    # even when botocore is unavailable.
    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass


# Base name for the default output file. When --output is omitted, a timestamp
# suffix is appended (see default_output_path) so repeated runs do not overwrite
# each other.
DEFAULT_OUTPUT_BASENAME = "support-breakdown-report"

# The Enterprise Support billing APIs are served from us-east-1, so the region
# is fixed rather than user-configurable.
BILLING_REGION = "us-east-1"

# Billing client methods this report depends on. They are relatively new, so an
# older boto3/botocore (which can happen on a local machine) may not expose
# them. We check for them up front to fail with a clear, actionable message.
REQUIRED_CLIENT_METHODS = (
    "get_enterprise_support_contract_details",
    "get_enterprise_support_charge_summary",
    "list_enterprise_support_linked_account_charges",
)

# Column -> data source mapping:
#   description                  <- contract pricingPlans[0].description
#   charge_allocation_method     <- contract supportAllocationMethod
#   charge_account_id            <- linked account payerAccountId
#   total_aws_charges            <- summary totalSupportEligibleSpend
#   support_charges              <- summary totalSupportCharge * chargePercentage
#                                   (only on a charged payer's own row, else 0)
#   total_support_charges        <- summary totalSupportCharge
#   support_charge_percentage    <- support_charges / total_support_charges
#                                   (fraction billed to this row's charge account)
#   account_id                   <- linked account accountId
#   payer_account_id             <- linked account payerAccountId
#   account_total_charges        <- linked account totalSupportEligibleSpend (4 dp)
#   account_prorated_charges     <- linked account proratedTotalSupportEligibleSpend
#   account_total_seconds        <- linked account totalSeconds
#   account_billable_seconds     <- linked account billableSeconds
#   account_ri_charges           <- linked account totalSupportEligibleReservedInstanceSpend
#   account_sp_charges           <- linked account totalSupportEligibleSavingsPlanSpend
#   account_link_periods         <- linked account linkedTimePeriods (formatted)
#   account_subscription_periods <- linked account subscriptionTimePeriods (formatted)
#   bill_month                   <- the billing month being reported (YYYY-MM)
FIELDNAMES = [
    "description",
    "charge_allocation_method",
    "charge_account_id",
    "total_aws_charges",
    "support_charges",
    "total_support_charges",
    "support_charge_percentage",
    "account_id",
    "payer_account_id",
    "account_total_charges",
    "account_prorated_charges",
    "account_total_seconds",
    "account_billable_seconds",
    "account_ri_charges",
    "account_sp_charges",
    "account_link_periods",
    "account_subscription_periods",
    "bill_month",
]


# --------------------------------------------------------------------------- #
# Formatting helpers
#
# The billing APIs return monetary amounts as numeric strings with many decimal
# places. These helpers render them with trailing zeros stripped (and a specific
# rounding for a couple of columns) to keep the CSV compact and consistent.
# --------------------------------------------------------------------------- #
def _to_decimal(value):
    """Convert an API value (usually a numeric string) to Decimal, or None.

    Returns None for missing/empty values so callers can render them as an
    empty CSV cell.
    """
    if value is None or value == "":
        return None
    return Decimal(str(value))


def fmt_decimal(value, decimals=None):
    """Format a numeric value stripping trailing zeros, without exponent notation.

    Args:
        value: The raw value (numeric string, number, or None).
        decimals: If given, the value is first rounded half-up to this many
            decimal places. Used for ``account_total_charges`` (4 dp).

    Returns:
        A fixed-point string with no superfluous trailing zeros
        (e.g. "186162.0" -> "186162", "0.0" -> "0"), or "" for empty input.
    """
    d = _to_decimal(value)
    if d is None:
        return ""
    if decimals is not None:
        quant = Decimal(1).scaleb(-decimals)  # 10 ** -decimals, e.g. 0.0001
        d = d.quantize(quant, rounding=ROUND_HALF_UP)
    # normalize() removes trailing zeros but may produce exponent form
    # (e.g. Decimal("2678400").normalize() -> 2.6784E+6). The "f" format spec
    # forces plain fixed-point notation.
    d = d.normalize()
    return f"{d:f}"


def _fmt_int(value):
    """Format an integer value (e.g. seconds) as a plain string, or "" if empty."""
    if value is None or value == "":
        return ""
    return str(int(value))


def _parse_iso(value):
    """Parse an ISO-8601 timestamp string, tolerating a trailing 'Z' for UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fmt_java_datetime(value):
    """Format a timestamp like Java's ``Date.toString()``.

    Example: 'Tue Aug 06 21:10:25 UTC 2024'.

    boto3 deserializes the API's ``beginDate``/``endDate`` fields into
    ``datetime`` objects, so those are formatted directly. ISO-8601 strings are
    also accepted for robustness. Timestamps are in UTC, so the zone is rendered
    as a literal 'UTC'.
    """
    dt = value if isinstance(value, datetime) else _parse_iso(str(value))
    return dt.strftime("%a %b %d %H:%M:%S UTC %Y")


def fmt_periods(periods):
    """Format a list of ``{beginDate, endDate}`` periods into a single cell.

    Each period renders as 'begin - end'. When a period has no end date the
    result is 'begin -' (with a trailing dash), matching the source extract.
    Multiple periods are joined with '; '.
    """
    parts = []
    for period in periods or []:
        begin = period.get("beginDate")
        end = period.get("endDate")
        begin_s = fmt_java_datetime(begin) if begin else ""
        end_s = fmt_java_datetime(end) if end else ""
        if end_s:
            parts.append(f"{begin_s} - {end_s}")
        else:
            # No end date: keep the trailing " -" to signal an open period.
            parts.append(f"{begin_s} -")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# Row construction
# --------------------------------------------------------------------------- #
def _is_proportional_billing(allocation_method, charged):
    """Return True when the contract uses Proportional billing.

    ``allocation_method`` is the contract's ``supportAllocationMethod`` value.
    It has two valid values: ``Proportional`` and ``Fixed_Percentage``.
    Proportional means support charges are distributed to each account in
    proportion to its eligible spend. Fixed_Percentage means support charges
    are distributed across accounts according to pre-configured percentages
    from the contract.

    ``charged`` maps charged payer account id -> chargePercentage (as returned
    by the contract's ``chargedPayerAccountIds``).

    Billing is treated as Proportional when either signal indicates it:
      * ``supportAllocationMethod`` is ``Proportional``; or
      * there is at least one charged account and every ``chargePercentage`` is
        0.0. In Proportional billing the contract carries no per-account share,
        so all percentages come back as 0.0 and the charge must instead be
        distributed in proportion to each account's eligible spend.

    An empty ``charged`` map is not, on its own, treated as proportional: with
    no charged accounts there is nothing to signal proportional billing.
    """
    if allocation_method == "Proportional":
        return True
    if not charged:
        return False
    return all((_to_decimal(pct) or Decimal(0)) == 0 for pct in charged.values())


def build_rows(month, contract, summary, linked_accounts):
    """Combine the three API payloads into breakdown rows for one billing month.

    Args:
        month: Billing month string (YYYY-MM) used for the ``bill_month`` column.
        contract: Response from get-enterprise-support-contract-details.
        summary: Response from get-enterprise-support-charge-summary.
        linked_accounts: The ``linkedAccount`` list from
            list-enterprise-support-linked-account-charges.

    Returns:
        A list of row dicts (keyed by FIELDNAMES), sorted by payer account and
        then by account id.
    """
    # Contract-level fields shared by every row. Prefer the pricing plan on the
    # contract; fall back to the effective plan reported by the summary.
    pricing_plans = contract.get("pricingPlans") or []
    description = pricing_plans[0].get("description", "") if pricing_plans else ""
    if not description:
        effective_plan = summary.get("supportEffectivePricingPlan") or {}
        description = effective_plan.get("description", "")

    allocation_method = contract.get("supportAllocationMethod", "")

    # Month-level totals shared by every row.
    total_aws_charges = fmt_decimal(summary.get("totalSupportEligibleSpend"))
    total_support_charge_raw = summary.get("totalSupportCharge")
    total_support_charges = fmt_decimal(total_support_charge_raw)

    total_support_charge_dec = _to_decimal(total_support_charge_raw) or Decimal(0)

    # Map of charged payer accounts -> charge percentage, keyed by account id,
    # from the contract's chargedPayerAccountIds.
    #
    # supportAllocationMethod has two billing behaviours:
    #   * Fixed_Percentage - chargePercentage holds the pre-configured per-account
    #     share, so the charge for a charged account is
    #     total_support_charge * chargePercentage.
    #   * Proportional - every chargePercentage is 0.0. In this mode the charge
    #     is NOT read from the contract; it is distributed to each account in
    #     proportion to that account's eligible (prorated) spend.
    #
    # Proportional billing is selected either from the contract's
    # supportAllocationMethod value or from the data itself (charged accounts
    # whose chargePercentage values are all 0.0). See _is_proportional_billing.
    charged = {}
    for entry in contract.get("chargedPayerAccountIds") or []:
        charged[entry["accountId"]] = entry.get("chargePercentage", "0")

    proportional = _is_proportional_billing(allocation_method, charged)

    # For proportional billing, precompute the total prorated eligible spend
    # across all linked accounts. Each account's share of the total support
    # charge is its prorated spend divided by this total.
    total_prorated_spend = Decimal(0)
    if proportional:
        for acct in linked_accounts:
            prorated = _to_decimal(acct.get("proratedTotalSupportEligibleSpend"))
            if prorated is not None:
                total_prorated_spend += prorated

    # First pass: compute the support charge for each linked account, and
    # accumulate it per payer account. support_charges / support_charge_percentage
    # are reported aggregated per payer account, so every row belonging to the
    # same payer shows that payer's total charge (the sum across all of its
    # accounts) and its share of the total support charge.
    per_payer_support_charge = {}
    for acct in linked_accounts:
        account_id = acct.get("accountId", "")
        payer_account_id = acct.get("payerAccountId", "")

        if proportional:
            # Proportional billing: allocate the total support charge to every
            # account by its share of the total prorated eligible spend. Guard
            # against a zero total to avoid division by zero.
            prorated = _to_decimal(acct.get("proratedTotalSupportEligibleSpend")) or Decimal(0)
            if total_prorated_spend:
                account_charge = total_support_charge_dec * prorated / total_prorated_spend
            else:
                account_charge = Decimal(0)
        else:
            # Fixed_Percentage: the charge lands on the account that is itself a
            # charged (charge) account, using its chargePercentage. Every other
            # account contributes 0.
            charge_pct = charged.get(account_id)
            if charge_pct is not None:
                account_charge = total_support_charge_dec * _to_decimal(charge_pct)
            else:
                account_charge = Decimal(0)

        per_payer_support_charge[payer_account_id] = (
            per_payer_support_charge.get(payer_account_id, Decimal(0)) + account_charge
        )

    rows = []
    for acct in linked_accounts:
        account_id = acct.get("accountId", "")
        payer_account_id = acct.get("payerAccountId", "")

        # support_charges is aggregated per payer account: the sum of the
        # charges of every account under this row's payer. All rows of the same
        # payer therefore carry the same value.
        payer_support_charge = per_payer_support_charge.get(payer_account_id, Decimal(0))
        support_charges = fmt_decimal(payer_support_charge, decimals=2)

        # support_charge_percentage is the fraction of the total support charge
        # billed to this row's payer account (payer support charge / total). It
        # is derived from the amounts, so it is consistent regardless of
        # allocation method. Guard against a zero total to avoid division by zero.
        if total_support_charge_dec:
            support_charge_percentage = fmt_decimal(
                payer_support_charge / total_support_charge_dec
            )
        else:
            support_charge_percentage = "0"

        rows.append(
            {
                "description": description,
                "charge_allocation_method": allocation_method,
                # The extract uses the account's own payer id as the charge account.
                "charge_account_id": payer_account_id,
                "total_aws_charges": total_aws_charges,
                "support_charges": support_charges,
                "total_support_charges": total_support_charges,
                "support_charge_percentage": support_charge_percentage,
                "account_id": account_id,
                "payer_account_id": payer_account_id,
                # account_total_charges is rounded to 4 decimal places; all
                # other amounts are only stripped of trailing zeros.
                "account_total_charges": fmt_decimal(
                    acct.get("totalSupportEligibleSpend"), decimals=4
                ),
                "account_prorated_charges": fmt_decimal(
                    acct.get("proratedTotalSupportEligibleSpend")
                ),
                "account_total_seconds": _fmt_int(acct.get("totalSeconds")),
                "account_billable_seconds": _fmt_int(acct.get("billableSeconds")),
                "account_ri_charges": fmt_decimal(
                    acct.get("totalSupportEligibleReservedInstanceSpend")
                ),
                "account_sp_charges": fmt_decimal(
                    acct.get("totalSupportEligibleSavingsPlanSpend")
                ),
                "account_link_periods": fmt_periods(acct.get("linkedTimePeriods")),
                "account_subscription_periods": fmt_periods(
                    acct.get("subscriptionTimePeriods")
                ),
                "bill_month": month,
            }
        )

    # Group rows by payer account, with accounts ascending within each group.
    # Account ids are compared as strings to preserve leading zeros.
    rows.sort(key=lambda r: (r["payer_account_id"], r["account_id"]))
    return rows


# --------------------------------------------------------------------------- #
# Data sources
# --------------------------------------------------------------------------- #
def get_billing_client(profile):
    """Create a boto3 ``billing`` client, optionally using a named profile.

    The region is fixed to BILLING_REGION (us-east-1), where these APIs live.
    """
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("billing", region_name=BILLING_REGION)


def fetch_month_live(client, month):
    """Call the three billing APIs for a single billing month.

    The linked-account list is paginated, so we follow ``nextToken`` until all
    pages have been collected.

    Returns:
        A ``(contract, summary, linked_accounts)`` tuple.
    """
    contract = client.get_enterprise_support_contract_details(billingMonth=month)
    summary = client.get_enterprise_support_charge_summary(billingMonth=month)

    linked_accounts = []
    params = {"billingMonth": month}
    while True:
        response = client.list_enterprise_support_linked_account_charges(**params)
        linked_accounts.extend(response.get("linkedAccount", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
        params["nextToken"] = next_token

    return contract, summary, linked_accounts


# --------------------------------------------------------------------------- #
# Billing month helpers
# --------------------------------------------------------------------------- #
def default_end_month():
    """Return the most recent complete billing month (the previous calendar month).

    The current month is excluded because its billing data is not yet final.
    """
    first_of_this_month = datetime.now(timezone.utc).replace(day=1)
    last_complete = first_of_this_month - timedelta(days=1)
    return last_complete.strftime("%Y-%m")


def month_range(end_month, lookback):
    """Return ``lookback`` billing months ending at ``end_month``.

    The list is ordered most-recent first. Month arithmetic wraps across year
    boundaries (e.g. month_range("2026-02", 3) -> ["2026-02", "2026-01", "2025-12"]).
    """
    year, month = (int(part) for part in end_month.split("-"))
    months = []
    for offset in range(lookback):
        m = month - offset
        y = year
        # Roll back into the previous year(s) when the month index goes <= 0.
        while m <= 0:
            m += 12
            y -= 1
        months.append(f"{y:04d}-{m:02d}")
    return months


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def default_output_path():
    """Build the default output filename with a timestamp suffix.

    Example: 'support-breakdown-report-20260820-143005.csv'. The timestamp
    prevents repeated runs from overwriting a previous report.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{DEFAULT_OUTPUT_BASENAME}-{timestamp}.csv"


def write_csv(rows, output):
    """Write rows to ``output`` as CSV (path, or '-' for stdout).

    CRLF (``\r\n``) line endings are used, following the CSV convention.
    """
    if output == "-":
        writer = csv.DictWriter(sys.stdout, fieldnames=FIELDNAMES, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
        return

    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    # Progress goes to stderr so that '--output -' keeps stdout as pure CSV.
    print(f"Wrote {len(rows)} row(s) to {output}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
# A billing month must be exactly YYYY-MM with a four-digit year and a
# two-digit month in the 01-12 range.
_BILLING_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def billing_month_arg(value):
    """argparse ``type`` that enforces a strict ``YYYY-MM`` billing month."""
    if not _BILLING_MONTH_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"invalid billing month '{value}'; expected format YYYY-MM "
            "(e.g. 2026-07)"
        )
    return value


def positive_int_arg(value):
    """argparse ``type`` that requires an integer of at least 1."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            f"must be an integer >= 1 (got {parsed})"
        )
    return parsed


def parse_args(argv=None):
    """Define and parse the command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate a Support Breakdown Report (CSV)."
    )
    parser.add_argument(
        "--lookback",
        type=positive_int_arg,
        default=1,
        help="Number of billing months to include, counting back from the end "
        "month. Must be >= 1 (default: 1).",
    )
    parser.add_argument(
        "--end-month",
        default=None,
        type=billing_month_arg,
        help="Most recent billing month to include, as YYYY-MM "
        "(default: last complete calendar month).",
    )
    parser.add_argument(
        "--billing-month",
        default=None,
        type=billing_month_arg,
        help="Report a single billing month (YYYY-MM). Overrides "
        "--lookback/--end-month.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output CSV path, or '-' for stdout. When omitted, writes to "
        "support-breakdown-report-<timestamp>.csv in the current directory.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS named profile to use (default: environment/default profile).",
    )
    return parser.parse_args(argv)


def resolve_months(args):
    """Determine the list of billing months to report from the parsed args.

    A single ``--billing-month`` takes precedence; otherwise a look-back window
    is built from ``--end-month`` (or the last complete month) and ``--lookback``.
    """
    if args.billing_month:
        return [args.billing_month]
    end_month = args.end_month or default_end_month()
    # --lookback is validated to be >= 1 by positive_int_arg.
    return month_range(end_month, args.lookback)


def main(argv=None):
    """Entry point: fetch each month's data and write the combined CSV report."""
    args = parse_args(argv)
    months = resolve_months(args)

    if boto3 is None:
        # boto3 ships with AWS CloudShell; local users may need to install it.
        sys.exit(
            "boto3 is required to run this report. Install it with "
            "'pip install boto3' (it is already available in AWS CloudShell)."
        )

    # Creating the session/client can also fail (e.g. an unknown --profile
    # raises ProfileNotFound), so it is guarded just like the API calls below.
    try:
        client = get_billing_client(args.profile)
    except (ClientError, BotoCoreError) as exc:
        sys.exit(f"Failed to initialize the AWS billing client: {exc}")

    # Guard against an outdated boto3/botocore that predates these APIs.
    missing = [m for m in REQUIRED_CLIENT_METHODS if not hasattr(client, m)]
    if missing:
        sys.exit(
            "This boto3/botocore version does not support the Enterprise "
            "Support billing APIs (" + ", ".join(missing) + "). Upgrade with "
            "'pip install --upgrade boto3 botocore'."
        )

    all_rows = []
    had_errors = False
    for month in months:
        try:
            contract, summary, linked_accounts = fetch_month_live(client, month)
            if not linked_accounts:
                print(
                    f"WARN: no linked account charges returned for {month}",
                    file=sys.stderr,
                )
            month_rows = build_rows(month, contract, summary, linked_accounts)
        except (ClientError, BotoCoreError) as exc:
            # API-level failure (e.g. no contract that month, throttling). A
            # single month failing should not abort the whole report.
            print(f"WARN: skipping {month} (API error): {exc}", file=sys.stderr)
            had_errors = True
            continue
        except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
            # Unexpected or malformed data while building rows for this month.
            print(
                f"WARN: skipping {month} (could not build rows): {exc}",
                file=sys.stderr,
            )
            had_errors = True
            continue
        all_rows.extend(month_rows)

    # Resolve the output target: honor an explicit --output verbatim, otherwise
    # use a timestamped default filename so runs do not overwrite each other.
    output = args.output if args.output is not None else default_output_path()

    # Always write whatever was produced so partial results are not lost.
    write_csv(all_rows, output)

    # Signal failure to callers/automation with a non-zero exit code when
    # nothing was produced, or when any month was skipped due to an error.
    if not all_rows:
        sys.exit("No data was produced for the requested month(s).")
    if had_errors:
        sys.exit("Completed with errors: one or more months were skipped.")


if __name__ == "__main__":
    main()
