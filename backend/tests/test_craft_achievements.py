import datetime
import unittest
from pathlib import Path

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.social import ACHIEVEMENTS, FULL_BINDER_MIN_PAGES, _load_user_stats
    from database import Base
    from models import Binder, BinderCard, BinderSlot, Card, CollectionItem, User

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "achievement dependencies are unavailable")
class CraftAchievementTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(username="ash", hashed_password="x", role="trainer", is_active=True)
        self.other_user = User(username="misty", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.user, self.other_user])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _add_owned_card(self, card_id, *, quantity=1, variant="Normal", rarity="Common", artist=None):
        self.db.add(Card(
            id=card_id,
            tcg_card_id=card_id,
            name=card_id,
            number=card_id,
            lang="en",
            rarity=rarity,
            artist=artist,
            is_custom=False,
        ))
        self.db.flush()
        self.db.add(CollectionItem(
            card_id=card_id,
            user_id=self.user.id,
            quantity=quantity,
            condition="NM",
            variant=variant,
            lang="en",
            added_at=datetime.datetime.utcnow(),
        ))

    def _add_slot(self, binder, card_id, page, pocket):
        entry = BinderCard(binder_id=binder.id, card_id=card_id, required_quantity=1)
        self.db.add(entry)
        self.db.flush()
        self.db.add(BinderSlot(
            binder_card_id=entry.id,
            binder_id=binder.id,
            page=page,
            pocket=pocket,
        ))

    def test_craft_metrics_are_case_insensitive_and_count_only_the_owner(self):
        self._add_owned_card("holo", quantity=100, variant="hOlO", artist="Holo Artist")
        self._add_owned_card("reverse", quantity=100, variant="reverse holo", artist="Reverse Artist")
        self._add_owned_card("edition", variant="FIRST EDITION", artist="Edition Artist")
        self._add_owned_card("rare", quantity=100, rarity="rArE", artist="Rare Artist")
        self._add_owned_card("ultra", rarity="ULTRA RARE", artist="Ultra Artist")
        self._add_owned_card("secret", rarity="secret rare", artist="Secret Artist")
        for index in range(44):
            self._add_owned_card(f"artist-{index}", artist=f"Artist {index}")
        self.db.commit()

        complete_page_binder = Binder(
            name="Complete page",
            user_id=self.user.id,
            binder_type="collection",
            grid_rows=3,
            grid_columns=3,
        )
        other_binder = Binder(
            name="Other trainer",
            user_id=self.other_user.id,
            binder_type="collection",
            grid_rows=2,
            grid_columns=2,
        )
        self.db.add_all([complete_page_binder, other_binder])
        self.db.commit()
        for pocket in range(1, 10):
            self._add_slot(complete_page_binder, "holo", page=1, pocket=pocket)
        self._add_slot(complete_page_binder, "reverse", page=2, pocket=1)
        for pocket in range(1, 5):
            self._add_slot(other_binder, "holo", page=1, pocket=pocket)
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]

        self.assertEqual(stats["holo_cards"], 100)
        self.assertEqual(stats["reverse_holo_cards"], 100)
        self.assertEqual(stats["first_edition_flag"], 1)
        self.assertEqual(stats["rare_cards"], 100)
        self.assertEqual(stats["ultra_rare_flag"], 1)
        self.assertEqual(stats["secret_rare_flag"], 1)
        self.assertEqual(stats["artist_diversity"], 50)
        self.assertEqual(stats["complete_binder_page_flag"], 1)
        self.assertEqual(stats["full_binder_flag"], 0)

        # Completing the second page is still not a full binder. Without a
        # floor, "every page up to the last one in use" is satisfied by a
        # single complete page, and the full-binder milestone would be granted
        # at the same moment as the complete-page one.
        for pocket in range(2, 10):
            self._add_slot(complete_page_binder, "holo", page=2, pocket=pocket)
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]
        self.assertEqual(stats["complete_binder_page_flag"], 1)
        self.assertEqual(stats["full_binder_flag"], 0)

        for page in (3, 4):
            for pocket in range(1, 10):
                self._add_slot(complete_page_binder, "holo", page=page, pocket=pocket)
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]
        self.assertEqual(stats["full_binder_flag"], 1)

        # Adding a card to a later page must not take the milestone away.
        # Measuring every page up to the highest one in use did exactly that:
        # a single card on page five revoked an achievement already earned.
        self._add_slot(complete_page_binder, "holo", page=6, pocket=1)
        self.db.commit()
        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]
        self.assertEqual(stats["full_binder_flag"], 1)

        # A gap inside the promised range still disqualifies it.
        self.db.query(BinderSlot).filter(
            BinderSlot.binder_id == complete_page_binder.id,
            BinderSlot.page == 3,
            BinderSlot.pocket == 5,
        ).delete()
        self.db.commit()
        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]
        self.assertEqual(stats["full_binder_flag"], 0)

    def test_rows_of_zero_or_fewer_are_not_cards_anybody_owns(self):
        # card_state counts a variant as owned only while its quantity is
        # positive. A zero-quantity row unlocked a first-of-its-kind
        # milestone, and a negative one subtracted from the counted ones.
        self._add_owned_card("zero-edition", quantity=0, variant="First Edition")
        self._add_owned_card("zero-ultra", quantity=0, rarity="Ultra Rare")
        self._add_owned_card("zero-secret", quantity=0, rarity="Secret Rare")
        self._add_owned_card("zero-artist", quantity=0, artist="Ghost Artist")
        self._add_owned_card("real-holo", quantity=10, variant="Holo", artist="Real Artist")
        self._add_owned_card("negative-holo", quantity=-5, variant="Holo")
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]

        self.assertEqual(stats["first_edition_flag"], 0)
        self.assertEqual(stats["ultra_rare_flag"], 0)
        self.assertEqual(stats["secret_rare_flag"], 0)
        self.assertEqual(stats["artist_diversity"], 1)
        self.assertEqual(stats["holo_cards"], 10)

    def test_a_legacy_binder_with_no_type_still_earns_its_milestone(self):
        # NULL is a collection binder made before the column existed, which is
        # how binder_allocations and the binder update path both read it.
        # Testing only for "collection" silently denied every legacy binder.
        legacy = Binder(
            name="Legacy",
            user_id=self.user.id,
            binder_type=None,
            grid_rows=3,
            grid_columns=3,
        )
        self.db.add(legacy)
        self.db.commit()
        # binder_type has a column default of "collection", so passing None to
        # the constructor stores "collection" and a test written that way
        # proves nothing. Force the NULL after the insert.
        self.db.query(Binder).filter(Binder.id == legacy.id).update(
            {Binder.binder_type: None}, synchronize_session=False
        )
        self.db.commit()
        self.assertIsNone(
            self.db.query(Binder.binder_type).filter(Binder.id == legacy.id).scalar()
        )
        for page in range(1, 5):
            for pocket in range(1, 10):
                self._add_slot(legacy, "holo", page=page, pocket=pocket)
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]

        self.assertEqual(stats["complete_binder_page_flag"], 1)
        self.assertEqual(stats["full_binder_flag"], 1)

    def test_a_filled_wishlist_binder_earns_no_binder_milestone(self):
        # A wishlist binder lays out cards the user does not own. Counting it
        # granted both binder milestones to someone owning nothing at all.
        wishlist_binder = Binder(
            name="Wanted",
            user_id=self.user.id,
            binder_type="wishlist",
            grid_rows=3,
            grid_columns=3,
        )
        self.db.add(wishlist_binder)
        self.db.commit()
        for page in range(1, 5):
            for pocket in range(1, 10):
                self._add_slot(wishlist_binder, "holo", page=page, pocket=pocket)
        self.db.commit()

        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]

        self.assertEqual(stats["complete_binder_page_flag"], 0)
        self.assertEqual(stats["full_binder_flag"], 0)

    def test_achievement_catalogue_has_all_craft_milestones_and_valid_badges(self):
        expected_ids = {
            "holo_hunter_10",
            "holo_hunter_50",
            "holo_hunter_100",
            "reverse_holo_hunter_10",
            "reverse_holo_hunter_50",
            "reverse_holo_hunter_100",
            "first_edition",
            "rare_hunter_10",
            "rare_hunter_50",
            "rare_hunter_100",
            "ultra_rare_finder",
            "secret_rare_finder",
            "complete_binder_page",
            "full_binder",
            "artist_explorer_10",
            "artist_explorer_25",
            "artist_explorer_50",
        }

        self.assertEqual(len(ACHIEVEMENTS), 37)
        self.assertTrue(expected_ids.issubset({achievement["id"] for achievement in ACHIEVEMENTS}))
        self.assertEqual(len({achievement["id"] for achievement in ACHIEVEMENTS}), len(ACHIEVEMENTS))
        badge_ids = [achievement["badge_id"] for achievement in ACHIEVEMENTS]
        self.assertEqual(len(set(badge_ids)), len(badge_ids))
        self.assertTrue(all(1 <= badge_id <= 77 for badge_id in badge_ids))

        # The binder milestone's copy names its threshold, because a binder
        # does not record how many pages it has and "complete" would claim
        # something the data cannot establish. Changing the constant without
        # the copy would leave the app promising the wrong thing.
        self.assertEqual(
            FULL_BINDER_MIN_PAGES,
            4,
            "FULL_BINDER_MIN_PAGES changed: update fullBinder and fullBinderDesc in en.js to match",
        )

        english = Path(__file__).parents[2] / "frontend" / "src" / "i18n" / "en.js"
        translation_source = english.read_text()
        for achievement in ACHIEVEMENTS:
            for key in (achievement["name_key"], achievement["description_key"]):
                field = key.split(".", maxsplit=1)[1]
                self.assertIn(f"    {field}:", translation_source)


if __name__ == "__main__":
    unittest.main()
