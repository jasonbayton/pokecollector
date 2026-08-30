import datetime
import unittest
from pathlib import Path

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.social import ACHIEVEMENTS, _load_user_stats
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

        # A gap below the last page in use still disqualifies it.
        self._add_slot(complete_page_binder, "holo", page=6, pocket=1)
        self.db.commit()
        stats = _load_user_stats(self.db, [self.user.id])[self.user.id]
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

        english = Path(__file__).parents[2] / "frontend" / "src" / "i18n" / "en.js"
        translation_source = english.read_text()
        for achievement in ACHIEVEMENTS:
            for key in (achievement["name_key"], achievement["description_key"]):
                field = key.split(".", maxsplit=1)[1]
                self.assertIn(f"    {field}:", translation_source)


if __name__ == "__main__":
    unittest.main()
