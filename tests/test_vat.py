"""Tests for determine_line_item_vat in extractor.py."""

import pytest
from extractor import (
    DiscountType,
    InvoiceDiscount,
    RawInvoice,
    RawLineItem,
    VAT_RATE,
    determine_line_item_vat,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def inv(**kwargs) -> RawInvoice:
    defaults = dict(
        supplier_name="Test Supplier",
        invoice_date="2026-01-01",
        subtotal=sum(i.total_price for i in kwargs.get("line_items", [])),
        total_amount=0,
        tax_amount=None,
        global_discounts=[],
        declared_vat_base=None,
        declared_non_vat_base=None,
    )
    defaults.update(kwargs)
    return RawInvoice(**defaults)


def item(price: float, name: str = "Item") -> RawLineItem:
    return RawLineItem(
        ingredient_name=name,
        quantity=1,
        unit="pcs",
        unit_price=price,
        total_price=price,
    )


def disc(dtype: DiscountType, amount: float) -> InvoiceDiscount:
    return InvoiceDiscount(discount_type=dtype, amount=amount)


# ---------------------------------------------------------------------------
# Simple invoices — no declared bases
# ---------------------------------------------------------------------------


def test_all_vatable_when_tax_present():
    """All items get VAT when tax_amount > 0 and no bases are declared."""
    invoice = inv(line_items=[item(100), item(200), item(300)], tax_amount=42.0)
    result = determine_line_item_vat(invoice)
    assert result == {
        0: (True, round(100 * VAT_RATE, 2)),
        1: (True, round(200 * VAT_RATE, 2)),
        2: (True, round(300 * VAT_RATE, 2)),
    }


def test_all_exempt_when_tax_is_zero():
    """All items exempt when tax_amount == 0."""
    invoice = inv(line_items=[item(100), item(200)], tax_amount=0.0)
    result = determine_line_item_vat(invoice)
    assert result == {0: (False, 0.0), 1: (False, 0.0)}


def test_all_exempt_when_tax_is_missing():
    """All items exempt when tax_amount is None (supplier didn't print a VAT line)."""
    invoice = inv(line_items=[item(659.0)], tax_amount=None)
    result = determine_line_item_vat(invoice)
    assert result == {0: (False, 0.0)}


# ---------------------------------------------------------------------------
# Mixed invoices — VAT base declared
# ---------------------------------------------------------------------------


def test_vat_base_identifies_single_vatable_item():
    """Declared VAT base matches one item exactly; remaining items are exempt."""
    invoice = inv(
        line_items=[item(100), item(200), item(300)],
        tax_amount=21.0,
        declared_vat_base=300.0,
    )
    result = determine_line_item_vat(invoice)
    assert result[0] == (False, 0.0)
    assert result[1] == (False, 0.0)
    assert result[2] == (True, round(300.0 * VAT_RATE, 2))


def test_both_bases_resolve_ambiguous_subset():
    """
    When VAT base is ambiguous (300 = item[2] OR items[0]+item[1]),
    declaring both bases forces the unique solution.
    """
    invoice = inv(
        line_items=[item(100), item(200), item(300)],
        tax_amount=21.0,
        declared_vat_base=300.0,
        declared_non_vat_base=300.0,
    )
    result = determine_line_item_vat(invoice)
    assert result[2] == (True, round(300.0 * VAT_RATE, 2))
    assert result[0] == (False, 0.0)
    assert result[1] == (False, 0.0)


def test_cross_check_rejects_combo_whose_remainder_doesnt_match():
    """
    A combo that matches vat_base but whose remainder doesn't match
    non_vat_base must be skipped in favour of the correct combo.
    Items: 50, 100, 200. Only item[2]=200 satisfies both constraints.
    """
    invoice = inv(
        line_items=[item(50), item(100), item(200)],
        tax_amount=14.0,
        declared_vat_base=200.0,
        declared_non_vat_base=150.0,
    )
    result = determine_line_item_vat(invoice)
    assert result[0] == (False, 0.0)
    assert result[1] == (False, 0.0)
    assert result[2] == (True, round(200.0 * VAT_RATE, 2))


# ---------------------------------------------------------------------------
# Mixed invoices — only non-VAT base declared
# ---------------------------------------------------------------------------


def test_exempt_base_inverts_to_find_vatable_items():
    """When only non-VAT base is declared, items outside that subset are VATable."""
    invoice = inv(
        line_items=[item(100), item(200), item(300)],
        tax_amount=21.0,
        declared_non_vat_base=300.0,
    )
    result = determine_line_item_vat(invoice)
    assert result[2] == (False, 0.0)
    assert result[0] == (True, round(100 * VAT_RATE, 2))
    assert result[1] == (True, round(200 * VAT_RATE, 2))


# ---------------------------------------------------------------------------
# Discount handling
# ---------------------------------------------------------------------------


def test_lump_sum_discount_reduces_effective_price_before_vat():
    """
    LUMP_SUM discount is distributed proportionally across items.
    VAT is then calculated on the reduced effective prices.
    Items: 100, 200. Discount: 30. Effective: 90, 180.
    """
    invoice = inv(
        line_items=[item(100), item(200)],
        tax_amount=18.9,
        global_discounts=[disc(DiscountType.LUMP_SUM, 30.0)],
    )
    result = determine_line_item_vat(invoice)
    eff0 = 100 - (100 / 300) * 30  # 90
    eff1 = 200 - (200 / 300) * 30  # 180
    assert result[0] == (True, round(eff0 * VAT_RATE, 2))
    assert result[1] == (True, round(eff1 * VAT_RATE, 2))


def test_lump_sum_discount_effective_prices_used_for_subset_match():
    """
    On a mixed invoice with a LUMP_SUM discount, the subset search runs
    against effective (post-discount) prices, not raw prices.
    Items: 200, 400. Discount: 60. Effective: 180, 360.
    declared_vat_base=360 matches effective item[1].
    """
    invoice = inv(
        line_items=[item(200), item(400)],
        tax_amount=25.2,
        declared_vat_base=360.0,
        global_discounts=[disc(DiscountType.LUMP_SUM, 60.0)],
    )
    result = determine_line_item_vat(invoice)
    eff1 = 400 - (400 / 600) * 60  # 360
    assert result[0] == (False, 0.0)
    assert result[1] == (True, round(eff1 * VAT_RATE, 2))


def test_exempt_only_discount_lowers_exempt_target():
    """
    EXEMPT_ONLY discount reduces declared_non_vat_base before subset search.
    Raw non_vat_base=230, discount=30 → adjusted target=200 → item[1] is exempt.
    """
    invoice = inv(
        line_items=[item(100), item(200), item(300)],
        tax_amount=28.0,
        declared_non_vat_base=230.0,
        global_discounts=[disc(DiscountType.EXEMPT_ONLY, 30.0)],
    )
    result = determine_line_item_vat(invoice)
    assert result[1] == (False, 0.0)
    assert result[0] == (True, round(100 * VAT_RATE, 2))
    assert result[2] == (True, round(300 * VAT_RATE, 2))


def test_vatable_only_discount_lowers_vat_target():
    """
    VATABLE_ONLY discount reduces declared_vat_base before subset search.
    Raw vat_base=230, discount=30 → adjusted target=200 → item[1] is VATable.
    """
    invoice = inv(
        line_items=[item(100), item(200), item(300)],
        tax_amount=14.0,
        declared_vat_base=230.0,
        global_discounts=[disc(DiscountType.VATABLE_ONLY, 30.0)],
    )
    result = determine_line_item_vat(invoice)
    assert result[1] == (True, round(200 * VAT_RATE, 2))
    assert result[0] == (False, 0.0)
    assert result[2] == (False, 0.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_unresolvable_base_defaults_all_items_to_exempt():
    """When no subset matches the declared base, all items fall back to exempt."""
    invoice = inv(
        line_items=[item(100), item(200)],
        tax_amount=10.0,
        declared_vat_base=999.0,
    )
    result = determine_line_item_vat(invoice)
    assert result == {0: (False, 0.0), 1: (False, 0.0)}


def test_rounding_tolerance_handles_fractional_supplier_prices():
    """Subset sum within 0.05 of target still matches (handles supplier rounding)."""
    # 92.52 + 83.18 = 175.70, target = 175.68 (within tolerance)
    invoice = inv(
        line_items=[item(92.52), item(83.18), item(500.0)],
        tax_amount=12.3,
        declared_vat_base=175.68,
        declared_non_vat_base=500.0,
    )
    result = determine_line_item_vat(invoice)
    assert result[0] == (True, round(92.52 * VAT_RATE, 2))
    assert result[1] == (True, round(83.18 * VAT_RATE, 2))
    assert result[2] == (False, 0.0)
