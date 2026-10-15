import asyncio
from unittest.mock import AsyncMock, MagicMock

from features.classroom import Classroom


async def main():
    bot = MagicMock()
    classroom = Classroom(bot)

    # Test 1: Reuse an existing role.
    existing_role = MagicMock()
    existing_role.name = "Professor"

    guild = MagicMock()
    guild.roles = [existing_role]
    guild.create_role = AsyncMock()

    result = await classroom._ensure_classroom_role(
        guild, "Professor"
    )

    assert result is existing_role
    guild.create_role.assert_not_awaited()

    # Test 2: Create a role when it doesn't exist.
    guild.roles = []
    new_role = MagicMock()
    guild.create_role = AsyncMock(return_value=new_role)

    result = await classroom._ensure_classroom_role(
        guild, "TA"
    )

    assert result is new_role
    guild.create_role.assert_awaited_once_with(
        name="TA",
        reason="TaigaBot classroom setup",
    )

    print("All classroom role tests passed!")


asyncio.run(main())