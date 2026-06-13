from pydantic import BaseModel, Field
from itertools import combinations
from pydantic_ai import Agent, BinaryContent
from typing import Literal, Optional
from enum import Enum

from datetime import date as Date

VAT_RATE = 0.07


class DiscountType(str, Enum):
    LUMP_SUM = "LUMP_SUM"  # Proportionally discounts all item prices
    EXEMPT_ONLY = "EXEMPT_ONLY"  # Discount only on non-vat items
    VATABLE_ONLY = "VATABLE_ONLY"  # Discount only on vat items


# Doesnt exst (yet, just showing extendability)
#    SHIPPING = "SHIPPING_DISCOUNT"  # (Easy to add later without schema migrations)


class InvoiceDiscount(BaseModel):
    discount_type: DiscountType = Field(
        description="Categorize how this discount is applied based on the document text."
    )
    amount: float
    raw_text: Optional[str] = Field(
        default=None,
        description="The exact text printed (in English and exact language if different than english) on the invoice next to the discount.",
    )


class RawLineItem(BaseModel):
    ingredient_name: str
    quantity: float
    unit: str
    unit_price: float
    total_price: float


class RawInvoice(BaseModel):
    invoice_id: Optional[str] = None
    supplier_name: str
    invoice_date: str  # YYYY-MM-DD
    due_date: Optional[Date] = None
    line_items: list[RawLineItem]
    subtotal: float
    tax_amount: Optional[float] = Field(
        default=None, description="The total 7% VAT amount from the totals box"
    )
    total_amount: float
    global_discounts: list[InvoiceDiscount] = Field(default_factory=list)
    declared_vat_base: Optional[float] = Field(
        default=None,
        description="The subtotal field of VAT-eligible items on the invoice (before VAT is added)",
    )
    declared_non_vat_base: Optional[float] = Field(
        default=None,
        description="The subtotal field of VAT-exempt items on the invoice",
    )


Category = Literal[
    "Cleaning Supplies",
    "Kitchen Supplies",
    "COGS Food",
    "Equipment: Kitchen",
    "Equipment: Operation",
    "Supplies: Operation",
]

Subcategory = Literal["Food Ingredients", "Food Packaging"]


class ItemTaxonomy(BaseModel):
    category: Category
    subcategory: Optional[Subcategory] = None


class RefinedLineItem(BaseModel):
    ingredient_name: str
    quantity: float
    unit: str
    unit_price: float
    total_price: float
    category: Category
    subcategory: Optional[Subcategory] = None
    is_vat_eligible: bool
    calculated_vat: float


class RefinedInvoice(BaseModel):
    invoice_id: Optional[str] = None
    supplier_name: str
    invoice_date: str
    due_date: Optional[Date] = None
    global_discounts: list[InvoiceDiscount] = Field(default_factory=list)
    line_items: list[RefinedLineItem]
    subtotal: float
    tax_amount: float
    total_amount: float
    math_validated: bool


def determine_line_item_vat(raw_invoice: RawInvoice) -> dict[int, tuple[bool, float]]:
    items = raw_invoice.line_items
    num_items = len(items)
    results: dict[int, tuple[bool, float]] = {i: (False, 0.0) for i in range(num_items)}

    target_vat_base = raw_invoice.declared_vat_base
    target_non_vat_base = raw_invoice.declared_non_vat_base

    gross_calculated_subtotal = sum(item.total_price for item in items)
    total_lump_sum_discount = 0.0

    # 1. Discounts: Adjust the targets based on the global discounts
    for discount in raw_invoice.global_discounts:
        if discount.discount_type == DiscountType.LUMP_SUM:
            total_lump_sum_discount += discount.amount
        elif discount.discount_type == DiscountType.EXEMPT_ONLY:
            if target_non_vat_base is not None:
                target_non_vat_base -= discount.amount
        elif discount.discount_type == DiscountType.VATABLE_ONLY:
            if target_vat_base is not None:
                target_vat_base -= discount.amount

    # 2. Compute the "Effective Price" for each item after a proportional global discount
    # If there is no discount, effective_prices is just exactly the item.total_price
    effective_prices = []
    for item in items:
        if gross_calculated_subtotal > 0 and total_lump_sum_discount > 0:
            weight = item.total_price / gross_calculated_subtotal
            item_discount = weight * total_lump_sum_discount
            effective_prices.append(item.total_price - item_discount)
        else:
            effective_prices.append(item.total_price)

    # Case 1: Simple invoice (Everything is VATable, or everything is Exempt)
    if not target_vat_base and not target_non_vat_base:
        is_all_vat = raw_invoice.tax_amount is not None and raw_invoice.tax_amount > 0
        for i in range(num_items):
            vat = round(effective_prices[i] * VAT_RATE, 2) if is_all_vat else 0.0
            results[i] = (is_all_vat, vat)
        return results

    # Case 2: Mixed-VAT invoice (Match against VAT base using the EFFECTIVE prices)
    if target_vat_base and target_vat_base > 0:
        indices = list(range(num_items))

        for r in range(1, num_items + 1):
            for combo in combinations(indices, r):
                combo_sum = sum(effective_prices[i] for i in combo)

                # Tolerance increased slightly to account for floating point
                # rounding during the proportional discount distribution
                if abs(combo_sum - target_vat_base) < 0.05:
                    if target_non_vat_base:
                        remaining_sum = sum(
                            effective_prices[i] for i in indices if i not in combo
                        )
                        if abs(remaining_sum - target_non_vat_base) > 0.05:
                            continue
                    for i in combo:
                        results[i] = (True, round(effective_prices[i] * VAT_RATE, 2))
                    return results

    # Case 3: Match against Non-VAT base using EFFECTIVE prices
    if target_non_vat_base and target_non_vat_base > 0:
        indices = list(range(num_items))
        for r in range(1, num_items + 1):
            for combo in combinations(indices, r):
                combo_sum = sum(effective_prices[i] for i in combo)
                if abs(combo_sum - target_non_vat_base) < 0.05:
                    if target_vat_base:
                        remaining_sum = sum(
                            effective_prices[i] for i in indices if i not in combo
                        )
                        if abs(remaining_sum - target_vat_base) > 0.05:
                            continue
                    for i in range(num_items):
                        if i in combo:
                            results[i] = (False, 0.0)
                        else:
                            results[i] = (
                                True,
                                round(effective_prices[i] * VAT_RATE, 2),
                            )
                    return results

    return results


product_invoice_agent = Agent(
    "google-gla:gemini-2.5-flash",
    output_type=list[RawInvoice],
    system_prompt=(
        "You extract structured data from restaurant supplier purchase invoices only. A receipt is a proof of payment given to a customer — do NOT extract it. If a page is a receipt, ignore it completely and do not include it in your output."
        "Extract structured values exactly as written."
        "Do not calculate tax or categorize products yourself.\n"
        "If the invoice shows separate VAT base and exempt base amounts, extract them into "
        "declared_vat_base and declared_non_vat_base.\n"
        "Translate product names and supplier names to English."
        "If the original is in another language, translate everything to English."
    ),
)

taxonomy_agent = Agent(
    "google-gla:gemini-2.5-flash",
    output_type=list[ItemTaxonomy],
    system_prompt=(
        "You are an inventory accountant. For each line item, assign a category and optional subcategory:"
        "Categories:\n"
        "- 'Cleaning Supplies': cleaning products, detergents, sanitizers, mops, sponges, etc\n"
        "- 'Kitchen Supplies': disposable kitchen items, gloves, plastic wrap, containers, etc\n"
        "- 'COGS Food': food ingredients and food packaging materials\n"
        "- 'Equipment: Kitchen': kitchen appliances, cooking tools, utensils, etc\n"
        "- 'Equipment: Operation': routers, printers, storage, etc\n"
        "- 'Supplies: Operation': office/POS equipment, non-kitchen operational hardware, etc\n"
        "Subcategories (assign when applicable, otherwise omit):\n"
        "- 'Food Ingredients': physical raw ingredients only — produce, meat, seafood, dairy, dry goods, spices. NOT services, labor, or processing fees — use with 'COGS Food'\n"
        "- 'Food Packaging': boxes, bags, wrapping for food — use with 'COGS Food'\n"
        "Return exactly one result per item in the same order"
    ),
)

TAXONOMY_CACHE: dict[str, ItemTaxonomy] = {}


def get_taxonomies(raw_items: list[RawLineItem]) -> list[ItemTaxonomy]:
    """Classify all uncached items in a single API call."""
    uncached = [
        (i, item)
        for i, item in enumerate(raw_items)
        if item.ingredient_name.strip().upper() not in TAXONOMY_CACHE
    ]

    if uncached:
        names = [item.ingredient_name for _, item in uncached]
        prompt = "Categorize each item in order:\n" + "\n".join(f"- {n}" for n in names)
        res = taxonomy_agent.run_sync(prompt)
        for (_, item), taxonomy in zip(uncached, res.output):
            TAXONOMY_CACHE[item.ingredient_name.strip().upper()] = taxonomy

    return [TAXONOMY_CACHE[item.ingredient_name.strip().upper()] for item in raw_items]


def extract_invoice(file_bytes: bytes, media_type: str) -> list[RefinedInvoice]:
    result = product_invoice_agent.run_sync(
        [
            BinaryContent(data=file_bytes, media_type=media_type),
            "Extract ALL invoices in this document. "
            "If multiple pages belong to the same supplier and invoice, combine them into ONE invoice with all line items merged. "
            "Only create separate invoices when the supplier or transaction is genuinely different.",
        ]
    )
    raw_invoices: list[RawInvoice] = result.output

    # Pass 3: Process Taxonomy and map to final state
    refined = []
    for raw in raw_invoices:
        if not raw.line_items:
            continue

        # Pass 2: Per-item VAT determination
        vat_map = determine_line_item_vat(raw)

        # Pass 3: Taxonomy — one API call for all uncached items
        taxonomies = get_taxonomies(raw.line_items)

        refined_items = [
            RefinedLineItem(
                ingredient_name=item.ingredient_name,
                quantity=item.quantity,
                unit=item.unit,
                unit_price=item.unit_price,
                total_price=item.total_price,
                category=taxonomies[i].category,
                subcategory=taxonomies[i].subcategory,
                is_vat_eligible=vat_map[i][0],
                calculated_vat=vat_map[i][1],
            )
            for i, item in enumerate(raw.line_items)
        ]

        calculated_subtotal = sum(item.total_price for item in raw.line_items)
        math_valid = abs(calculated_subtotal - raw.subtotal) < 0.02

        refined.append(
            RefinedInvoice(
                invoice_id=raw.invoice_id,
                supplier_name=raw.supplier_name,
                invoice_date=raw.invoice_date,
                due_date=raw.due_date,
                line_items=refined_items,
                subtotal=raw.subtotal,
                tax_amount=raw.tax_amount or 0.0,
                total_amount=raw.total_amount,
                math_validated=math_valid,
                global_discounts=raw.global_discounts,
            )
        )

    return refined
