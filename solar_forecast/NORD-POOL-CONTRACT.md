# LSF Nord Pool contract

LSF uses one site-level contract for electricity price data. A geographic
proposal is informative only and must never become a dispatch input until the
area is confirmed.

```yaml
nord_pool:
  enabled: true
  bidding_area:
    proposed: "NO1"
    confirmed: "NO1"
    confirmation_status: "confirmed" # proposed | confirmed | rejected
    confidence: "high"               # high | medium | low
    basis: "site_geodata"
    confirmed_by: "site_owner"
    confirmed_at: "2026-08-02T00:00:00+02:00"
  price:
    source: "home_assistant_core_nordpool"
    integration_domain: "nordpool"
    entity: "sensor.nord_pool_no1_current_price"
    tomorrow_available_entity: "binary_sensor.nord_pool_no1_tomorrow_price_available"
    last_updated_entity: "sensor.nord_pool_no1_last_updated"
    currency: "NOK"
    unit: "NOK/kWh"
    price_semantics: "raw_day_ahead_market_price"
    vat_included: false
    grid_tariff_included: false
    taxes_included: false
    supplier_markup_included: false
    granularity: "source_native"
    current_value: "state"
    prices_for_date_action: "nordpool.get_prices_for_date"
    price_indices_for_date_action: "nordpool.get_price_indices_for_date"
    interval_contract: "use_returned_start_and_end; never_assume_60_minutes"
```

## Approval gate

- `confirmation_status: proposed` is shown to the user but is not approved for
  battery or export control.
- Dispatch requires `confirmation_status: confirmed`, matching non-null
  `proposed` and `confirmed` areas, and a live price entity whose currency and
  unit match this contract.
- A location near a bidding-area boundary, missing geodata, conflicting HA
  location and address, or an unknown price entity always requires explicit
  user confirmation.
- Changing geodata invalidates the confirmation until the area is checked
  again.
- The Home Assistant Core integration is the preferred source. Its price is
  the raw market price; VAT, grid tariff, taxes and supplier markup belong in
  separately named derived-price layers.
- LSF fetches day series through `nordpool.get_prices_for_date`, stores source
  timestamps and returned interval boundaries, and must not silently convert
  hourly and 15-minute values into each other.

The five valid Norwegian areas are `NO1`, `NO2`, `NO3`, `NO4` and `NO5`.
Statnett remains the authority for the current bidding-area definition. LSF's
geographic result is deliberately called a proposal because grid boundaries
cannot be derived safely from rough latitude/longitude rectangles.
