from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from api.auth import get_current_user
from api.cards import _card_to_dict
from database import get_db
from models import Binder, BinderSlot, Card, CollectionItem, ProductPurchase, Set, User, UserSetting, WishlistItem
from services.card_values import effective_market_price, normalize_price_field
from services.card_visibility import visible_card_filter
from services.digital_sets import digital_sets_enabled
from services.public_profile_feature import public_profiles_enabled
from services.portfolio_valuation import calculate_portfolio_valuation

router = APIRouter()


# How many complete pages a binder needs before it counts as full. See the
# comment where it is used: the model cannot know a binder's real page count.
FULL_BINDER_MIN_PAGES = 4


ACHIEVEMENTS = [
    {
        "id": "first_card",
        "name_key": "achievements.firstCard",
        "description_key": "achievements.firstCardDesc",
        "badge_id": 1,
        "metric": "total_cards",
        "target": 1,
    },
    {
        "id": "collector_10",
        "name_key": "achievements.collector10",
        "description_key": "achievements.collector10Desc",
        "badge_id": 2,
        "metric": "total_cards",
        "target": 10,
    },
    {
        "id": "collector_50",
        "name_key": "achievements.collector50",
        "description_key": "achievements.collector50Desc",
        "badge_id": 3,
        "metric": "total_cards",
        "target": 50,
    },
    {
        "id": "collector_100",
        "name_key": "achievements.collector100",
        "description_key": "achievements.collector100Desc",
        "badge_id": 4,
        "metric": "total_cards",
        "target": 100,
    },
    {
        "id": "collector_500",
        "name_key": "achievements.collector500",
        "description_key": "achievements.collector500Desc",
        "badge_id": 5,
        "metric": "total_cards",
        "target": 500,
    },
    {
        "id": "collector_1000",
        "name_key": "achievements.collector1000",
        "description_key": "achievements.collector1000Desc",
        "badge_id": 6,
        "metric": "total_cards",
        "target": 1000,
    },
    {
        "id": "first_set",
        "name_key": "achievements.firstSet",
        "description_key": "achievements.firstSetDesc",
        "badge_id": 7,
        "metric": "sets_completed",
        "target": 1,
    },
    {
        "id": "set_master_5",
        "name_key": "achievements.setMaster5",
        "description_key": "achievements.setMaster5Desc",
        "badge_id": 8,
        "metric": "sets_completed",
        "target": 5,
    },
    {
        "id": "set_master_10",
        "name_key": "achievements.setMaster10",
        "description_key": "achievements.setMaster10Desc",
        "badge_id": 9,
        "metric": "sets_completed",
        "target": 10,
    },
    {
        "id": "big_spender_100",
        "name_key": "achievements.bigSpender100",
        "description_key": "achievements.bigSpender100Desc",
        "badge_id": 10,
        "metric": "total_value",
        "target": 100,
    },
    {
        "id": "big_spender_500",
        "name_key": "achievements.bigSpender500",
        "description_key": "achievements.bigSpender500Desc",
        "badge_id": 11,
        "metric": "total_value",
        "target": 500,
    },
    {
        "id": "big_spender_1000",
        "name_key": "achievements.bigSpender1000",
        "description_key": "achievements.bigSpender1000Desc",
        "badge_id": 12,
        "metric": "total_value",
        "target": 1000,
    },
    {
        "id": "big_spender_5000",
        "name_key": "achievements.bigSpender5000",
        "description_key": "achievements.bigSpender5000Desc",
        "badge_id": 13,
        "metric": "total_value",
        "target": 5000,
    },
    {
        "id": "investor",
        "name_key": "achievements.investor",
        "description_key": "achievements.investorDesc",
        "badge_id": 14,
        "metric": "positive_pnl_flag",
        "target": 1,
    },
    {
        "id": "diversifier",
        "name_key": "achievements.diversifier",
        "description_key": "achievements.diversifierDesc",
        "badge_id": 15,
        "metric": "set_diversity",
        "target": 10,
    },
    {
        "id": "diversifier_25",
        "name_key": "achievements.diversifier25",
        "description_key": "achievements.diversifier25Desc",
        "badge_id": 16,
        "metric": "set_diversity",
        "target": 25,
    },
    {
        "id": "wishlist_hunter",
        "name_key": "achievements.wishlistHunter",
        "description_key": "achievements.wishlistHunterDesc",
        "badge_id": 17,
        "metric": "wishlist_count",
        "target": 5,
    },
    {
        "id": "first_sale",
        "name_key": "achievements.firstSale",
        "description_key": "achievements.firstSaleDesc",
        "badge_id": 18,
        "metric": "sold_products_count",
        "target": 1,
    },
    {
        "id": "trader",
        "name_key": "achievements.trader",
        "description_key": "achievements.traderDesc",
        "badge_id": 19,
        "metric": "sold_products_count",
        "target": 10,
    },
    {
        "id": "rare_finder",
        "name_key": "achievements.rareFinder",
        "description_key": "achievements.rareFinderDesc",
        "badge_id": 20,
        "metric": "illustration_rare_flag",
        "target": 1,
    },
    {
        "id": "holo_hunter_10",
        "name_key": "achievements.holoHunter10",
        "description_key": "achievements.holoHunter10Desc",
        "badge_id": 21,
        "metric": "holo_cards",
        "target": 10,
    },
    {
        "id": "holo_hunter_50",
        "name_key": "achievements.holoHunter50",
        "description_key": "achievements.holoHunter50Desc",
        "badge_id": 22,
        "metric": "holo_cards",
        "target": 50,
    },
    {
        "id": "holo_hunter_100",
        "name_key": "achievements.holoHunter100",
        "description_key": "achievements.holoHunter100Desc",
        "badge_id": 23,
        "metric": "holo_cards",
        "target": 100,
    },
    {
        "id": "reverse_holo_hunter_10",
        "name_key": "achievements.reverseHoloHunter10",
        "description_key": "achievements.reverseHoloHunter10Desc",
        "badge_id": 24,
        "metric": "reverse_holo_cards",
        "target": 10,
    },
    {
        "id": "reverse_holo_hunter_50",
        "name_key": "achievements.reverseHoloHunter50",
        "description_key": "achievements.reverseHoloHunter50Desc",
        "badge_id": 25,
        "metric": "reverse_holo_cards",
        "target": 50,
    },
    {
        "id": "reverse_holo_hunter_100",
        "name_key": "achievements.reverseHoloHunter100",
        "description_key": "achievements.reverseHoloHunter100Desc",
        "badge_id": 26,
        "metric": "reverse_holo_cards",
        "target": 100,
    },
    {
        "id": "first_edition",
        "name_key": "achievements.firstEdition",
        "description_key": "achievements.firstEditionDesc",
        "badge_id": 27,
        "metric": "first_edition_flag",
        "target": 1,
    },
    {
        "id": "rare_hunter_10",
        "name_key": "achievements.rareHunter10",
        "description_key": "achievements.rareHunter10Desc",
        "badge_id": 28,
        "metric": "rare_cards",
        "target": 10,
    },
    {
        "id": "rare_hunter_50",
        "name_key": "achievements.rareHunter50",
        "description_key": "achievements.rareHunter50Desc",
        "badge_id": 29,
        "metric": "rare_cards",
        "target": 50,
    },
    {
        "id": "rare_hunter_100",
        "name_key": "achievements.rareHunter100",
        "description_key": "achievements.rareHunter100Desc",
        "badge_id": 30,
        "metric": "rare_cards",
        "target": 100,
    },
    {
        "id": "ultra_rare_finder",
        "name_key": "achievements.ultraRareFinder",
        "description_key": "achievements.ultraRareFinderDesc",
        "badge_id": 31,
        "metric": "ultra_rare_flag",
        "target": 1,
    },
    {
        "id": "secret_rare_finder",
        "name_key": "achievements.secretRareFinder",
        "description_key": "achievements.secretRareFinderDesc",
        "badge_id": 32,
        "metric": "secret_rare_flag",
        "target": 1,
    },
    {
        "id": "complete_binder_page",
        "name_key": "achievements.completeBinderPage",
        "description_key": "achievements.completeBinderPageDesc",
        "badge_id": 33,
        "metric": "complete_binder_page_flag",
        "target": 1,
    },
    {
        "id": "full_binder",
        "name_key": "achievements.fullBinder",
        "description_key": "achievements.fullBinderDesc",
        "badge_id": 34,
        "metric": "full_binder_flag",
        "target": 1,
    },
    {
        "id": "artist_explorer_10",
        "name_key": "achievements.artistExplorer10",
        "description_key": "achievements.artistExplorer10Desc",
        "badge_id": 35,
        "metric": "artist_diversity",
        "target": 10,
    },
    {
        "id": "artist_explorer_25",
        "name_key": "achievements.artistExplorer25",
        "description_key": "achievements.artistExplorer25Desc",
        "badge_id": 36,
        "metric": "artist_diversity",
        "target": 25,
    },
    {
        "id": "artist_explorer_50",
        "name_key": "achievements.artistExplorer50",
        "description_key": "achievements.artistExplorer50Desc",
        "badge_id": 37,
        "metric": "artist_diversity",
        "target": 50,
    },
]


def _card_payload(card: Card | None):
    """Catalogue detail for a card shown on someone else's profile.

    The card modal renders rarity, HP, artist, types and supertype, so a
    summary of just a name and a thumbnail leaves its overview blank. The raw
    custom_image_url stays behind: images resolve through the card-id proxy,
    and it is a user-supplied URL on another user's card.
    """
    if not card:
        return None
    payload = _card_to_dict(card)
    custom_image_url = payload.pop("custom_image_url", None)
    payload["has_custom_image_fallback"] = bool(
        custom_image_url and not (card.images_small or card.images_large)
    )
    return payload


def _load_user_stats(db: Session, user_ids: list[int] | None = None, price_field: str = "price_trend"):
    price_field = normalize_price_field(price_field)
    sharing_enabled = public_profiles_enabled(db)

    def _get_price(row):
        return effective_market_price(row, getattr(row, "variant", None), price_field)

    user_query = db.query(User).filter(User.is_active == True)
    if user_ids is not None:
        user_query = user_query.filter(User.id.in_(user_ids))
    users = user_query.order_by(User.username.asc()).all()
    if not users:
        return {}

    active_user_ids = [user.id for user in users]

    collection_query = db.query(
        CollectionItem.user_id,
        CollectionItem.card_id,
        CollectionItem.quantity,
        CollectionItem.purchase_price,
        CollectionItem.variant,
        Card.id.label("card_db_id"),
        Card.name,
        Card.images_small,
        Card.images_large,
        Card.data_source_lang,
        Card.price_source_lang,
        Card.image_source_lang,
        Card.custom_image_url,
        Card.price_market,
        Card.price_low,
        Card.price_trend,
        Card.price_avg1,
        Card.price_avg7,
        Card.price_avg30,
        Card.price_market_holo,
        Card.price_low_holo,
        Card.price_trend_holo,
        Card.price_avg1_holo,
        Card.price_avg7_holo,
        Card.price_avg30_holo,
        Card.set_id,
        Card.lang,
        Card.rarity,
        Card.artist,
    ).join(
        Card, CollectionItem.card_id == Card.id
    ).filter(
        CollectionItem.user_id.in_(active_user_ids),
        Card.is_custom == False,
    )
    if not digital_sets_enabled(db):
        collection_query = collection_query.filter(Card.is_digital == False)
    collection_rows = collection_query.all()

    set_sizes = {
        (row.set_id, row.lang): row.card_count
        for row in db.query(
            Card.set_id,
            Card.lang,
            func.count(Card.id).label("card_count"),
        ).filter(
            Card.set_id.isnot(None),
            Card.is_custom == False,
        ).group_by(
            Card.set_id, Card.lang
        ).all()
    }

    wishlist_query = db.query(
        WishlistItem.user_id,
        func.count(WishlistItem.id).label("count"),
    ).join(
        Card, WishlistItem.card_id == Card.id
    ).filter(
        WishlistItem.user_id.in_(active_user_ids),
        Card.is_custom == False,
    )
    if not digital_sets_enabled(db):
        wishlist_query = wishlist_query.filter(Card.is_digital == False)
    wishlist_counts = {
        row.user_id: row.count
        for row in wishlist_query.group_by(
            WishlistItem.user_id
        ).all()
    }

    sold_product_counts = {
        row.user_id: row.count
        for row in db.query(
            ProductPurchase.user_id,
            func.count(ProductPurchase.id).label("count"),
        ).filter(
            ProductPurchase.user_id.in_(active_user_ids),
            ProductPurchase.sold_price.isnot(None),
        ).group_by(
            ProductPurchase.user_id
        ).all()
    }

    binder_pages_by_user = defaultdict(lambda: defaultdict(dict))
    for row in db.query(
        Binder.user_id,
        Binder.id.label("binder_id"),
        Binder.grid_rows,
        Binder.grid_columns,
        BinderSlot.page,
        func.count(BinderSlot.id).label("slot_count"),
    ).join(
        BinderSlot, BinderSlot.binder_id == Binder.id
    ).filter(
        Binder.user_id.in_(active_user_ids),
        # A wishlist binder lays out cards the user does NOT own, so filling
        # one is not a collecting milestone. Without this, four filled
        # wishlist pages earned both binder achievements with nothing owned.
        #
        # NULL is a legacy collection binder, not an unknown kind: the rest of
        # the codebase reads it that way, in binder_allocations and when
        # updating a binder's type. Testing only for "collection" would have
        # quietly denied the milestone to every binder made before the column
        # existed.
        or_(Binder.binder_type == "collection", Binder.binder_type.is_(None)),
        Binder.grid_rows.isnot(None),
        Binder.grid_columns.isnot(None),
    ).group_by(
        Binder.user_id,
        Binder.id,
        Binder.grid_rows,
        Binder.grid_columns,
        BinderSlot.page,
    ).all():
        binder_pages_by_user[row.user_id][row.binder_id][row.page] = (
            row.slot_count,
            row.grid_rows * row.grid_columns,
        )

    complete_binder_page_users = set()
    full_binder_users = set()
    for user_id, binders in binder_pages_by_user.items():
        for pages in binders.values():
            if any(slot_count == capacity for slot_count, capacity in pages.values()):
                complete_binder_page_users.add(user_id)
            capacity = next(iter(pages.values()))[1]
            # A binder does not record how many pages it physically has, only
            # the geometry of one page, so this cannot mean "every page of the
            # binder". It means the first FULL_BINDER_MIN_PAGES pages, which
            # is what the milestone's name and description promise.
            #
            # Measuring every page up to the last one in use instead would
            # both fire on a single complete page, at the same moment as the
            # page milestone, and revoke itself the moment a card was placed
            # on a later page - taking an earned achievement away for adding
            # a card.
            if all(
                pages.get(page, (0, capacity))[0] == capacity
                for page in range(1, FULL_BINDER_MIN_PAGES + 1)
            ):
                full_binder_users.add(user_id)

    items_by_user = defaultdict(list)
    for row in collection_rows:
        items_by_user[row.user_id].append(row)

    stats = {}
    most_valuable_rows: dict[int, object] = {}

    for user in users:
        rows = items_by_user.get(user.id, [])
        # A collection row can remain after its quantity reaches zero (for
        # example after a partial sale or edit). It records history, not an
        # owned card. Keep every ownership-derived stat on this same positive
        # subset so badges, totals, set completion, and the highlight card
        # cannot disagree about whether the user owns a printing.
        owned_rows = [row for row in rows if (row.quantity or 0) > 0]
        total_cards = sum(row.quantity for row in owned_rows)
        unique_card_ids = {row.card_id for row in owned_rows}
        valuation = calculate_portfolio_valuation(db, user.id, price_field)

        most_valuable = None
        if owned_rows:
            most_valuable_rows[user.id] = max(owned_rows, key=lambda row: _get_price(row))

        owned_by_set = defaultdict(set)
        owned_set_ids = set()
        has_illustration_rare = False
        holo_cards = 0
        reverse_holo_cards = 0
        rare_cards = 0
        has_first_edition = False
        has_ultra_rare = False
        has_secret_rare = False
        artists = set()
        for row in owned_rows:
            if row.set_id:
                owned_by_set[(row.set_id, row.lang)].add(row.card_id)
                owned_set_ids.add(row.set_id)
            variant = (row.variant or "").strip().casefold()
            rarity = (row.rarity or "").strip().casefold()
            quantity = row.quantity
            if "illustration rare" in rarity:
                has_illustration_rare = True
            if variant == "holo":
                holo_cards += quantity
            elif variant == "reverse holo":
                reverse_holo_cards += quantity
            elif variant == "first edition":
                has_first_edition = True
            if rarity == "rare":
                rare_cards += quantity
            elif rarity == "ultra rare":
                has_ultra_rare = True
            elif rarity == "secret rare":
                has_secret_rare = True
            artist = (row.artist or "").strip()
            if artist:
                artists.add(artist.casefold())

        sets_completed = 0
        for set_key, owned_cards in owned_by_set.items():
            total_in_set = set_sizes.get(set_key, 0)
            if total_in_set > 0 and len(owned_cards) >= total_in_set:
                sets_completed += 1

        total_value = valuation.total_value
        total_cost = valuation.active_cost_basis
        pnl = valuation.total_pnl
        pnl_pct = (pnl / valuation.performance_cost_basis) * 100 if valuation.performance_cost_basis > 0 else None

        stats[user.id] = {
            "user_id": user.id,
            "username": user.username,
            "avatar_id": user.avatar_id,
            "role": user.role,
            "total_cards": total_cards,
            "unique_cards": len(unique_card_ids),
            "total_value": round(total_value, 2),
            "most_valuable_card": most_valuable,
            "sets_completed": sets_completed,
            "total_invested": round(total_cost, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "set_diversity": len(owned_set_ids),
            "wishlist_count": wishlist_counts.get(user.id, 0),
            "sold_products_count": sold_product_counts.get(user.id, 0),
            "positive_pnl_flag": 1 if pnl > 0 else 0,
            "illustration_rare_flag": 1 if has_illustration_rare else 0,
            "holo_cards": holo_cards,
            "reverse_holo_cards": reverse_holo_cards,
            "first_edition_flag": 1 if has_first_edition else 0,
            "rare_cards": rare_cards,
            "ultra_rare_flag": 1 if has_ultra_rare else 0,
            "secret_rare_flag": 1 if has_secret_rare else 0,
            "complete_binder_page_flag": 1 if user.id in complete_binder_page_users else 0,
            "full_binder_flag": 1 if user.id in full_binder_users else 0,
            "artist_diversity": len(artists),
            "public_handle": user.public_handle if sharing_enabled and user.is_profile_public else None,
        }

    if most_valuable_rows:
        card_ids = {row.card_db_id for row in most_valuable_rows.values()}
        cards = {
            card.id: card
            for card in db.query(Card)
            .options(joinedload(Card.set_ref))
            .filter(Card.id.in_(card_ids))
            .all()
        }
        for user_id, row in most_valuable_rows.items():
            payload = _card_payload(cards.get(row.card_db_id))
            if payload is None:
                continue
            # The row carries the variant-aware price and the language the copy
            # was actually valued in, which the card alone cannot know.
            payload.update({
                "id": row.card_db_id,
                "card_id": row.card_db_id,
                "price_market": round(_get_price(row), 2),
                "data_source_lang": row.data_source_lang,
                "price_source_lang": row.price_source_lang,
                "image_source_lang": row.image_source_lang,
            })
            stats[user_id]["most_valuable_card"] = payload

    return stats


@router.get("/leaderboard")
def get_leaderboard(
    price_field: str = Query(default="price_trend", description="Price field to use for value calculation"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stats = _load_user_stats(db, price_field=price_field)
    leaderboard = sorted(
        stats.values(),
        key=lambda entry: (entry["total_value"], entry["total_cards"], entry["unique_cards"]),
        reverse=True,
    )
    return leaderboard


@router.get("/compare/{user_id}")
def compare_users(
    user_id: int,
    price_field: str = Query(default="price_trend", description="Price field to use for value calculation"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot compare user to self")

    other_user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not other_user:
        raise HTTPException(status_code=404, detail="User not found")

    stats = _load_user_stats(db, [current_user.id, user_id], price_field=price_field)
    if current_user.id not in stats or user_id not in stats:
        raise HTTPException(status_code=404, detail="Comparison users not found")

    user_a_cards = {
        row.card_id: row
        for row in db.query(
            CollectionItem.card_id,
            CollectionItem.quantity,
            Card.name,
            Card.images_small,
            Card.images_large,
            Card.data_source_lang,
            Card.price_source_lang,
            Card.image_source_lang,
            Card.custom_image_url,
        ).join(
            Card, CollectionItem.card_id == Card.id
        ).filter(
            CollectionItem.user_id == current_user.id,
            Card.is_custom == False,
            visible_card_filter(db, current_user.id, "all"),
        ).all()
    }
    user_b_cards = {
        row.card_id: row
        for row in db.query(
            CollectionItem.card_id,
            CollectionItem.quantity,
            Card.name,
            Card.images_small,
            Card.images_large,
            Card.data_source_lang,
            Card.price_source_lang,
            Card.image_source_lang,
            Card.custom_image_url,
        ).join(
            Card, CollectionItem.card_id == Card.id
        ).filter(
            CollectionItem.user_id == user_id,
            Card.is_custom == False,
            visible_card_filter(db, user_id, "all"),
        ).all()
    }

    user_a_wishlist = {
        row.card_id
        for row in db.query(WishlistItem.card_id).join(Card, Card.id == WishlistItem.card_id).filter(
            WishlistItem.user_id == current_user.id,
            Card.is_custom == False,
            visible_card_filter(db, current_user.id, "all"),
        ).all()
    }
    user_b_wishlist = {
        row.card_id
        for row in db.query(WishlistItem.card_id).join(Card, Card.id == WishlistItem.card_id).filter(
            WishlistItem.user_id == user_id,
            Card.is_custom == False,
            visible_card_filter(db, user_id, "all"),
        ).all()
    }

    overlap = len(set(user_a_cards) & set(user_b_cards))
    only_a = len(set(user_a_cards) - set(user_b_cards))
    only_b = len(set(user_b_cards) - set(user_a_cards))

    trade_suggestions = []
    seen_pairs = set()

    for owner_stats, wants_stats, owner_cards, wanted_cards in [
        (stats[current_user.id], stats[user_id], user_a_cards, user_b_wishlist),
        (stats[user_id], stats[current_user.id], user_b_cards, user_a_wishlist),
    ]:
        for card_id, row in owner_cards.items():
            if row.quantity <= 1 or card_id not in wanted_cards:
                continue
            pair = (card_id, owner_stats["user_id"], wants_stats["user_id"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            trade_suggestions.append({
                "card_id": card_id,
                "card_name": row.name,
                "images_small": row.images_small,
                "data_source_lang": row.data_source_lang,
                "price_source_lang": row.price_source_lang,
                "image_source_lang": row.image_source_lang,
                "has_custom_image_fallback": bool(
                    row.custom_image_url and not (row.images_small or row.images_large)
                ),
                "owner_username": owner_stats["username"],
                "wants_username": wants_stats["username"],
            })
            if len(trade_suggestions) >= 10:
                break
        if len(trade_suggestions) >= 10:
            break

    return {
        "user_a": stats[current_user.id],
        "user_b": stats[user_id],
        "overlap": overlap,
        "only_a": only_a,
        "only_b": only_b,
        "trade_suggestions": trade_suggestions,
    }


@router.get("/achievements/{user_id}")
def get_achievements(
    user_id: int,
    price_field: str = Query(default="price_trend", description="Price field to use for value calculation"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    stats = _load_user_stats(db, [user_id], price_field=price_field).get(user_id)
    if not stats:
        raise HTTPException(status_code=404, detail="User stats not found")

    achievements = []
    for config in ACHIEVEMENTS:
        progress = stats.get(config["metric"], 0)
        unlocked = progress >= config["target"]
        achievements.append({
            "id": config["id"],
            "name_key": config["name_key"],
            "description_key": config["description_key"],
            "badge_id": config["badge_id"],
            "unlocked": unlocked,
            "progress": min(progress, config["target"]) if config["target"] == 1 else progress,
            "target": config["target"],
        })

    return {
        "user_id": user.id,
        "username": user.username,
        "avatar_id": user.avatar_id,
        "earned": sum(1 for achievement in achievements if achievement["unlocked"]),
        "total": len(achievements),
        "achievements": achievements,
    }


#: Per-user opt-in. Nobody's cards appear in the shared view until they choose
#: to contribute them, and opting in only affects what you contribute - viewing
#: is available to any signed-in user.
SHARE_COLLECTION_SETTING = "share_collection"


def merge_shared_collection_rows(rows, usernames, price_field) -> list[dict]:
    """Merge every contributed copy into one entry per card.

    Pure so the aggregation can be tested without a database: it decides
    who is shown as holding what, and which printings drive the foil
    effect.
    """
    merged: dict[str, dict] = {}
    for item, card in rows:
        # Zero and negative rows are a supported database state and are not
        # owned - card_state_summaries ignores them for exactly this reason.
        # Checked before the entry is created, or an empty row would invent an
        # owner and a printing for a card nobody holds.
        quantity = int(item.quantity or 0)
        if quantity <= 0:
            continue

        entry = merged.get(card.id)
        if entry is None:
            entry = {
                "id": card.id,
                "card_id": card.id,
                "card": _shared_card_payload(card),
                "quantity": 0,
                "owners": [],
                # Which printings exist across every owner. The card tile picks
                # one foil effect from these, so a card nobody holds in holo
                # should not shimmer.
                "variants": [],
                "total_value": 0.0,
            }
            merged[card.id] = entry

        variant = (item.variant or "").strip()
        if variant and variant not in entry["variants"]:
            entry["variants"].append(variant)
        price = effective_market_price(card, item.variant, price_field) or 0
        entry["quantity"] += quantity
        entry["total_value"] += float(price) * quantity

        # Keyed on username, and no user_id is returned: the page filters and
        # renders by name, so the id would be an identifier disclosed about
        # other people for no purpose.
        username = usernames.get(item.user_id, "?")
        owner = next((o for o in entry["owners"] if o["username"] == username), None)
        if owner is None:
            entry["owners"].append({"username": username, "quantity": quantity})
        else:
            owner["quantity"] += quantity

    data = sorted(merged.values(), key=lambda e: (-e["total_value"], e["card"]["name"] or ""))
    for entry in data:
        entry["owners"].sort(key=lambda o: o["username"].lower())
        entry["variants"].sort(key=str.lower)
        entry["total_value"] = round(entry["total_value"], 2)
        entry["owner_count"] = len(entry["owners"])
    return data


def _shared_card_payload(card: Card) -> dict:
    """The catalogue fields, minus anything that should not leave its owner.

    custom_image_url is a raw user-supplied URL. The public profile serialiser
    deliberately emits a proxied image and a boolean instead, and the shared
    view resolves images through the same card-id proxy, so the raw value has
    no reason to be here.
    """
    payload = _card_to_dict(card)
    custom_image_url = payload.pop("custom_image_url", None)
    payload["has_custom_image_fallback"] = bool(
        custom_image_url and not (card.images_small or card.images_large)
    )
    return payload


def _contributing_user_ids(db: Session) -> list[int]:
    """Active users who have opted their collection into the shared view."""
    opted_in = db.query(UserSetting.user_id).filter(
        UserSetting.key == SHARE_COLLECTION_SETTING,
        func.lower(func.trim(UserSetting.value)) == "true",
    ).all()
    ids = [row[0] for row in opted_in]
    if not ids:
        return []
    active = db.query(User.id).filter(User.id.in_(ids), User.is_active == True).all()
    return [row[0] for row in active]


@router.get("/server")
def get_server_collection(
    price_field: str = Query(default="price_trend"),
    lang: str | None = Query(default="all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every contributed collection, merged into one catalogue.

    One entry per card rather than one per owner: three people holding the same
    card reads as one card owned by three, which is the question this view
    exists to answer. Each entry carries who holds it and how many.
    """
    price_field = normalize_price_field(price_field)
    user_ids = _contributing_user_ids(db)
    empty = {
        "data": [],
        "total_cards": 0,
        "unique_cards": 0,
        "total_value": 0.0,
        "contributors": [],
    }
    if not user_ids:
        return empty

    usernames = {
        user.id: user.username
        for user in db.query(User).filter(User.id.in_(user_ids)).all()
    }

    rows = (
        db.query(CollectionItem, Card)
        .join(Card, Card.id == CollectionItem.card_id)
        # _card_to_dict reads card.set_ref, which would otherwise be a query per row
        .options(joinedload(Card.set_ref))
        .filter(
            CollectionItem.user_id.in_(user_ids),
            visible_card_filter(db, current_user.id, lang),
        )
        .all()
    )

    data = merge_shared_collection_rows(rows, usernames, price_field)

    return {
        "data": data,
        "total_cards": sum(e["quantity"] for e in data),
        "unique_cards": len(data),
        "total_value": round(sum(e["total_value"] for e in data), 2),
        "contributors": sorted(usernames.values(), key=str.lower),
    }
