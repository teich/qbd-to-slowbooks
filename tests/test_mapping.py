from qbd_to_slowbooks import accounts, journals, lists


def ref(list_id, name):
    return [{"ListID": [list_id], "FullName": [name]}]


def posting(txn, account, amount, **extra):
    rec = {
        "TxnID": [txn],
        "TxnType": [extra.pop("TxnType", "Check")],
        "TxnDate": [extra.pop("TxnDate", "2024-01-02")],
        "AccountRef": ref(*account),
        "Amount": [amount],
    }
    rec.update({k: [v] if isinstance(v, str) else v for k, v in extra.items()})
    return rec


BANK = ("A1", "Checking")
RENT = ("A2", "Rent")
ACCOUNTS = {"A1": 10, "A2": 20}


def test_entry_debits_positive_amounts_and_keeps_txn_id():
    result = journals.JournalResult()
    entries = journals.build_entries(
        [
            posting("T1", RENT, "100.00", RefNumber="1001", EntityRef=ref("V1", "Landlord")),
            posting("T1", BANK, "-100.00", Memo="January"),
        ],
        ACCOUNTS,
        result,
    )
    assert result.errors == []
    [entry] = entries
    assert entry["reference"] == "T1"
    assert entry["description"] == "Check 1001 · Landlord"
    assert entry["lines"] == [
        {"account_id": 20, "debit": "100.00", "credit": "0", "description": "Landlord"},
        {"account_id": 10, "debit": "0", "credit": "100.00", "description": "January"},
    ]


def test_non_posting_and_voided_transactions_are_dropped():
    result = journals.JournalResult()
    entries = journals.build_entries(
        [
            posting("E1", RENT, "5.00", TxnType="Estimate"),
            posting("E1", BANK, "-5.00", TxnType="Estimate"),
            posting("V1", RENT, "0.00"),
            posting("V1", BANK, "0.00"),
        ],
        ACCOUNTS,
        result,
    )
    assert entries == []
    assert result.empty == 1


def test_unbalanced_or_unmapped_transactions_are_refused():
    result = journals.JournalResult()
    entries = journals.build_entries(
        [
            posting("U1", RENT, "5.00"),
            posting("U1", BANK, "-4.00"),
            posting("M1", ("A9", "Unknown"), "5.00"),
            posting("M1", BANK, "-5.00"),
        ],
        ACCOUNTS,
        result,
    )
    assert entries == []
    assert len(result.errors) == 2
    assert "do not balance" in result.errors[0] or "do not balance" in result.errors[1]
    assert any("Unknown" in e for e in result.errors)


def test_balance_sheet_balances_become_debit_minus_credit():
    assert accounts.qb_balance_as_net({"AccountType": ["Bank"], "Balance": ["10.00"]}) == 10.0
    assert accounts.qb_balance_as_net({"AccountType": ["AccountsPayable"], "Balance": ["10.00"]}) == -10.0
    assert accounts.qb_balance_as_net({"AccountType": ["Income"], "Balance": ["10.00"]}) is None


def test_customer_takes_the_first_valid_email_and_full_name():
    body = lists.customer_body(
        {
            "Name": ["Job 1"],
            "FullName": ["Acme:Job 1"],
            "Email": ["ap@acme.com; buyer@acme.com"],
            "BillAddress": [{"Addr1": ["Acme"], "Addr2": ["1 Main St"], "Addr3": ["Suite 2"], "City": ["Reno"]}],
        }
    )
    assert body["name"] == "Acme:Job 1"
    assert body["email"] == "ap@acme.com"
    assert body["bill_address2"] == "1 Main St, Suite 2"
    assert body["bill_city"] == "Reno"


def test_service_item_reads_its_sales_or_purchase_block():
    body = lists.item_body(
        "ItemServiceRet",
        {
            "FullName": ["Refurbish"],
            "SalesOrPurchase": [{"Price": ["120.00"], "AccountRef": ref("A2", "Rent")}],
            "SalesTaxCodeRef": ref("T", "Non"),
        },
        ACCOUNTS,
    )
    assert body["item_type"] == "service"
    assert body["rate"] == "120.00"
    assert body["income_account_id"] == 20
    assert body["is_taxable"] is False
