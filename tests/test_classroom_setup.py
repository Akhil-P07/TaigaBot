import asyncio
import os
import tempfile

from database import Database


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "test.db"))

        await db.connect()
        try:
            # Existing servers default to club mode.
            club = await db.get_server_setup(111)
            assert club["setup_mode"] == "club"

            # Configure a classroom server.
            await db.set_server_setup(222, "classroom", "CSCI-243")

            classroom = await db.get_server_setup(222)
            assert classroom["setup_mode"] == "classroom"
            assert classroom["course_name"] == "CSCI-243"

            # The other server must remain unaffected.
            club = await db.get_server_setup(111)
            assert club["setup_mode"] == "club"

            # Reconfiguring the same server should update its record.
            await db.set_server_setup(222, "club")
            updated = await db.get_server_setup(222)

            assert updated["setup_mode"] == "club"
            assert updated["course_name"] == ""

            # Reject unsupported setup modes.
            try:
                await db.set_server_setup(333, "invalid")
                assert False, "Invalid setup mode was accepted"
            except ValueError:
                pass

            # Classroom mode must have a course name.
            try:
                await db.set_server_setup(333, "classroom", "")
                assert False, "Empty course name was accepted"
            except ValueError:
                pass
            
            print("All classroom configuration tests passed!")

        finally:
            await db.close()


asyncio.run(main())