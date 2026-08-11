"""Two people arranging the same binder at once.

PostgreSQL only. The rest of the slot tests repeat single-session cases against
a real database, which proves the constraints exist but says nothing about what
happens when two transactions compete. These open genuinely separate sessions.

The guarantee being checked is not that both requests succeed. It is that one
of them fails cleanly and no placement is lost, duplicated, or silently moved.
"""
import os
import threading
import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from api.binders import place_binder_slot
    from database import Base
    from models import Binder, BinderCard, BinderSlot, Card, CollectionItem, User
    from schemas import BinderSlotPlace
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class BinderSlotConcurrencyTests(unittest.TestCase):
    def setUp(self):
        database = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in database.lower():
            raise unittest.SkipTest(f"refusing to drop tables in database {database!r}")
        self.engine = create_engine(POSTGRES_TEST_URL)
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        setup = self.Session()
        user = User(username="jason", hashed_password="x", role="admin", is_active=True)
        setup.add(user)
        setup.commit()
        setup.refresh(user)
        self.user_id = user.id

        card = Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
                    set_id="sv1", number="1", lang="en")
        binder = Binder(name="Album", user_id=user.id, binder_type="collection",
                        grid_rows=3, grid_columns=3)
        setup.add_all([card, binder])
        setup.commit()
        setup.refresh(binder)
        self.binder_id = binder.id

        item = CollectionItem(card_id=card.id, user_id=user.id, quantity=4,
                              condition="NM", variant="Normal", lang="en")
        setup.add(item)
        setup.commit()
        setup.refresh(item)
        entry = BinderCard(binder_id=binder.id, card_id=card.id,
                           collection_item_id=item.id, required_quantity=4)
        setup.add(entry)
        setup.commit()
        setup.refresh(entry)
        self.entry_id = entry.id
        setup.close()

    def tearDown(self):
        self.engine.dispose()

    def _place_in_own_session(self, pocket, results, index):
        db = self.Session()
        try:
            user = db.query(User).filter(User.id == self.user_id).one()
            place_binder_slot(
                self.binder_id,
                BinderSlotPlace(binder_card_id=self.entry_id, page=1, pocket=pocket),
                current_user=user, db=db,
            )
            results[index] = "placed"
        except HTTPException as exc:
            results[index] = f"refused:{exc.status_code}"
        except IntegrityError:
            results[index] = "integrity"
        except Exception as exc:  # noqa: BLE001 - the point is to see anything else
            results[index] = f"unexpected:{type(exc).__name__}"
        finally:
            db.close()

    def test_two_people_cannot_fill_the_same_pocket(self):
        results = [None, None]
        threads = [
            threading.Thread(target=self._place_in_own_session, args=(1, results, index))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a placement hung")

        verify = self.Session()
        try:
            slots = verify.query(BinderSlot).filter(
                BinderSlot.binder_id == self.binder_id
            ).all()
        finally:
            verify.close()

        # Exactly one card in the pocket, and the loser was told so rather than
        # failing in some way the client cannot interpret.
        self.assertEqual(len(slots), 1, f"pocket filled {len(slots)} times: {results}")
        self.assertEqual(sorted(results), sorted(["placed", "refused:409"]), results)

    def test_two_people_filling_different_pockets_both_succeed(self):
        """The binder lock serialises them; it must not make one fail."""
        results = [None, None]
        threads = [
            threading.Thread(target=self._place_in_own_session, args=(pocket, results, index))
            for index, pocket in enumerate((1, 2))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a placement hung")

        verify = self.Session()
        try:
            count = verify.query(BinderSlot).filter(
                BinderSlot.binder_id == self.binder_id
            ).count()
        finally:
            verify.close()

        self.assertEqual(results, ["placed", "placed"], results)
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
