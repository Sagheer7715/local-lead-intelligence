"""Plain-language business types mapped to OpenStreetMap tags.

This is the usability hinge of Tier 1. A user says "dentists in Austin"; OSM
knows `amenity=dentist` and `healthcare=dentist`. Google Maps needs none of this
because it matches free text — the cost of using structured open data is that
somebody has to write the mapping down.

Each entry is a list of tag sets. A tag set is ANDed together and the sets are
ORed, so `[{"healthcare": "dentist", "healthcare:speciality": "orthodontics"}]`
is narrower than `[{"amenity": "dentist"}]`.

Unknown terms fall back to a name search, which is weaker but not useless —
see `name_fallback()`.
"""
from __future__ import annotations

# Canonical category -> OSM tag sets
TAGS = {
    # health
    "dentist":          [{"amenity": "dentist"}, {"healthcare": "dentist"}],
    "orthodontist":     [{"healthcare": "dentist", "healthcare:speciality": "orthodontics"}],
    "doctor":           [{"amenity": "doctors"}, {"healthcare": "doctor"}],
    "chiropractor":     [{"healthcare": "chiropractor"}],
    "physiotherapist":  [{"healthcare": "physiotherapist"}],
    "optician":         [{"shop": "optician"}, {"healthcare": "optometrist"}],
    "pharmacy":         [{"amenity": "pharmacy"}, {"healthcare": "pharmacy"}],
    "veterinary":       [{"amenity": "veterinary"}],
    "clinic":           [{"amenity": "clinic"}, {"healthcare": "clinic"}],

    # trades
    "plumber":          [{"craft": "plumber"}],
    "electrician":      [{"craft": "electrician"}],
    "roofer":           [{"craft": "roofer"}],
    "carpenter":        [{"craft": "carpenter"}],
    "painter":          [{"craft": "painter"}],
    "locksmith":        [{"craft": "locksmith"}],
    "hvac":             [{"craft": "hvac"}],
    "landscaper":       [{"craft": "gardener"}, {"shop": "garden_centre"}],
    "builder":          [{"craft": "builder"}, {"office": "construction_company"}],

    # professional services
    "lawyer":           [{"office": "lawyer"}],
    "accountant":       [{"office": "accountant"}],
    "insurance":        [{"office": "insurance"}],
    "real estate":      [{"office": "estate_agent"}],
    "architect":        [{"office": "architect"}],
    "marketing agency": [{"office": "advertising_agency"}, {"office": "marketing"}],
    "travel agency":    [{"shop": "travel_agency"}],
    "financial advisor": [{"office": "financial_advisor"}, {"office": "financial"}],

    # food and drink
    "cafe":             [{"amenity": "cafe"}],
    "coffee shop":      [{"amenity": "cafe", "cuisine": "coffee_shop"}, {"shop": "coffee"}],
    "restaurant":       [{"amenity": "restaurant"}],
    "bar":              [{"amenity": "bar"}],
    "pub":              [{"amenity": "pub"}],
    "bakery":           [{"shop": "bakery"}],
    "butcher":          [{"shop": "butcher"}],
    "caterer":          [{"craft": "caterer"}, {"shop": "deli"}],
    "fast food":        [{"amenity": "fast_food"}],

    # beauty and fitness
    "gym":              [{"leisure": "fitness_centre"}],
    "yoga studio":      [{"leisure": "fitness_centre", "sport": "yoga"}],
    "hairdresser":      [{"shop": "hairdresser"}],
    "barber":           [{"shop": "hairdresser", "hairdresser": "male"}, {"shop": "barber"}],
    "beauty salon":     [{"shop": "beauty"}],
    "nail salon":       [{"shop": "beauty", "beauty": "nails"}, {"shop": "nails"}],
    "spa":              [{"leisure": "spa"}, {"shop": "massage"}],
    "tattoo studio":    [{"shop": "tattoo"}],

    # retail and auto
    "car repair":       [{"shop": "car_repair"}],
    "car dealer":       [{"shop": "car"}],
    "car wash":         [{"amenity": "car_wash"}],
    "tyre shop":        [{"shop": "tyres"}],
    "florist":          [{"shop": "florist"}],
    "jeweller":         [{"shop": "jewelry"}],
    "furniture shop":   [{"shop": "furniture"}],
    "hardware shop":    [{"shop": "hardware"}, {"shop": "doityourself"}],
    "clothing shop":    [{"shop": "clothes"}],
    "pet shop":         [{"shop": "pet"}],
    "pet grooming":     [{"shop": "pet_grooming"}],
    "supermarket":      [{"shop": "supermarket"}],
    "dry cleaner":      [{"shop": "dry_cleaning"}, {"shop": "laundry"}],
    "photographer":     [{"craft": "photographer"}, {"shop": "photo"}],

    # other
    "hotel":            [{"tourism": "hotel"}],
    "guest house":      [{"tourism": "guest_house"}],
    "childcare":        [{"amenity": "childcare"}, {"amenity": "kindergarten"}],
    "driving school":   [{"amenity": "driving_school"}],
    "school":           [{"amenity": "school"}],
    "estate agent":     [{"office": "estate_agent"}],
    "funeral director": [{"shop": "funeral_directors"}],
    "storage":          [{"shop": "storage_rental"}],
    "coworking":        [{"amenity": "coworking_space"}, {"office": "coworking"}],
}

# Everything a user might plausibly type -> canonical key.
ALIASES = {
    "dentists": "dentist", "dental": "dentist", "dental practice": "dentist",
    "dental office": "dentist", "dentistry": "dentist",
    "orthodontists": "orthodontist", "pediatric dentist": "dentist",
    "doctors": "doctor", "gp": "doctor", "physician": "doctor",
    "physicians": "doctor", "medical practice": "doctor", "doctors office": "doctor",
    "chiropractors": "chiropractor", "chiro": "chiropractor",
    "physio": "physiotherapist", "physiotherapists": "physiotherapist",
    "physical therapist": "physiotherapist", "physical therapy": "physiotherapist",
    "opticians": "optician", "optometrist": "optician", "eye doctor": "optician",
    "pharmacies": "pharmacy", "chemist": "pharmacy", "drugstore": "pharmacy",
    "vet": "veterinary", "vets": "veterinary", "veterinarian": "veterinary",
    "veterinarians": "veterinary", "animal hospital": "veterinary",
    "clinics": "clinic",

    "plumbers": "plumber", "plumbing": "plumber",
    "electricians": "electrician", "electrical": "electrician",
    "roofers": "roofer", "roofing": "roofer",
    "carpenters": "carpenter", "joiner": "carpenter",
    "painters": "painter", "painting": "painter", "decorator": "painter",
    "locksmiths": "locksmith",
    "heating": "hvac", "air conditioning": "hvac", "ac repair": "hvac",
    "hvac contractor": "hvac",
    "landscapers": "landscaper", "landscaping": "landscaper",
    "gardener": "landscaper", "lawn care": "landscaper",
    "builders": "builder", "contractor": "builder", "construction": "builder",

    "lawyers": "lawyer", "attorney": "lawyer", "attorneys": "lawyer",
    "law firm": "lawyer", "solicitor": "lawyer", "solicitors": "lawyer",
    "accountants": "accountant", "cpa": "accountant", "bookkeeper": "accountant",
    "accounting": "accountant", "tax advisor": "accountant",
    "insurance agent": "insurance", "insurance broker": "insurance",
    "realtor": "real estate", "realtors": "real estate",
    "real estate agent": "real estate", "estate agents": "estate agent",
    "architects": "architect",
    "marketing agencies": "marketing agency", "ad agency": "marketing agency",
    "digital agency": "marketing agency", "advertising agency": "marketing agency",
    "travel agencies": "travel agency",
    "financial advisors": "financial advisor", "wealth manager": "financial advisor",

    "cafes": "cafe", "coffee": "coffee shop", "coffee shops": "coffee shop",
    "coffeeshop": "coffee shop", "espresso bar": "coffee shop",
    "restaurants": "restaurant", "bars": "bar", "pubs": "pub",
    "bakeries": "bakery", "butchers": "butcher",
    "caterers": "caterer", "catering": "caterer",
    "takeaway": "fast food", "takeout": "fast food",

    "gyms": "gym", "fitness": "gym", "fitness centre": "gym",
    "fitness center": "gym", "crossfit": "gym",
    "yoga": "yoga studio", "yoga studios": "yoga studio", "pilates": "yoga studio",
    "hairdressers": "hairdresser", "hair salon": "hairdresser",
    "hair salons": "hairdresser", "salon": "hairdresser",
    "barbers": "barber", "barbershop": "barber", "barber shop": "barber",
    "beauty salons": "beauty salon", "beautician": "beauty salon",
    "nail salons": "nail salon", "nails": "nail salon",
    "spas": "spa", "massage": "spa", "day spa": "spa",
    "tattoo": "tattoo studio", "tattoo parlour": "tattoo studio",
    "tattoo shops": "tattoo studio",

    "mechanic": "car repair", "mechanics": "car repair", "auto repair": "car repair",
    "garage": "car repair", "body shop": "car repair",
    "car dealers": "car dealer", "car dealership": "car dealer",
    "car wash": "car wash", "tire shop": "tyre shop", "tyres": "tyre shop",
    "florists": "florist", "flower shop": "florist",
    "jewellers": "jeweller", "jeweler": "jeweller", "jewelry store": "jeweller",
    "furniture store": "furniture shop", "hardware store": "hardware shop",
    "clothing store": "clothing shop", "boutique": "clothing shop",
    "pet store": "pet shop", "pet shops": "pet shop",
    "dog groomer": "pet grooming", "pet groomer": "pet grooming",
    "supermarkets": "supermarket", "grocery store": "supermarket",
    "dry cleaners": "dry cleaner", "laundromat": "dry cleaner",
    "photographers": "photographer", "wedding photographer": "photographer",

    "hotels": "hotel", "guest houses": "guest house", "bed and breakfast": "guest house",
    "b&b": "guest house",
    "daycare": "childcare", "nursery": "childcare", "preschool": "childcare",
    "driving schools": "driving school", "driving instructor": "driving school",
    "schools": "school",
    "funeral home": "funeral director", "funeral directors": "funeral director",
    "self storage": "storage", "storage units": "storage",
    "coworking space": "coworking",
}

# Words that carry no category meaning, so "best coffee shops near me" still works.
_NOISE_WORDS = {"the", "a", "an", "best", "top", "good", "cheap", "local", "near",
                "me", "my", "area", "nearby", "all", "list", "of", "and", "find",
                "service", "services", "company", "companies", "business",
                "businesses", "shop", "shops", "store", "stores"}


def _clean(text: str) -> str:
    words = [w for w in "".join(
        c if c.isalnum() or c in " &'" else " " for c in (text or "").lower()
    ).split() if w not in _NOISE_WORDS]
    return " ".join(words)


def resolve(term):
    """Map a plain-language business type to (canonical_name, tag_sets).

    Returns (None, None) when nothing matches, which is the caller's cue to use
    `name_fallback()` instead.
    """
    raw = (term or "").strip().lower()
    for candidate in (raw, _clean(raw)):
        if not candidate:
            continue
        if candidate in TAGS:
            return candidate, TAGS[candidate]
        if candidate in ALIASES:
            key = ALIASES[candidate]
            return key, TAGS[key]

    # Longest-first substring match, so "emergency plumbers" finds "plumbers"
    # and "coffee shop" is preferred over "coffee".
    cleaned = _clean(raw)
    for table in (ALIASES, TAGS):
        for key in sorted(table, key=len, reverse=True):
            if key in cleaned or key in raw:
                name = ALIASES[key] if table is ALIASES else key
                return name, TAGS[name]
    return None, None


def name_fallback(term):
    """Tag sets for an unmapped term: match the business name itself.

    `~` is an Overpass regex comparison, so this is a case-insensitive
    substring match on `name`. Much weaker than a real tag — it finds
    "Smith Upholstery" for "upholstery" but misses anything whose name does not
    contain the word.
    """
    needle = _clean(term) or (term or "").strip()
    if not needle:
        return []
    return [{"~name": needle}]


def known() -> list:
    return sorted(TAGS)
