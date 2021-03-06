import asyncio
import datetime as dt
import pickle
import re
import time
import urllib.parse
from contextlib import closing
from typing import List

import discord
from discord.ext import commands

from nabbot import NabBot
from utils import checks
from utils.config import config
from utils.database import tracked_worlds_list, userDatabase, tracked_worlds, get_server_property, set_server_property
from utils.discord import is_private, get_user_avatar
from utils.general import global_online_list, log, join_list, start_time
from utils.messages import weighed_choice, death_messages_player, death_messages_monster, format_message, EMOJI, \
    level_messages
from utils.paginator import Paginator, CannotPaginate, VocationPaginator
from utils.tibia import get_highscores, ERROR_NETWORK, tibia_worlds, get_world, get_character, get_voc_emoji, get_guild, \
    get_voc_abb, get_character_url, url_guild, \
    get_tibia_time_zone, NetworkError, Death, Character, HIGHSCORE_CATEGORIES, get_voc_abb_and_emoji


class Tracking:
    """Commands related to Nab Bot's tracking system."""

    def __init__(self, bot: NabBot):
        self.bot = bot
        self.scan_deaths_task = self.bot.loop.create_task(self.scan_deaths())
        self.scan_online_chars_task = bot.loop.create_task(self.scan_online_chars())
        self.scan_highscores_task = bot.loop.create_task(self.scan_highscores())

    async def scan_deaths(self):
        #################################################
        #             Nezune's cave                     #
        # Do not touch anything, enter at your own risk #
        #################################################
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(config.death_scan_interval)
                if len(global_online_list) == 0:
                    await asyncio.sleep(0.5)
                    continue
                # Pop last char in queue, reinsert it at the beginning
                current_char = global_online_list.pop()
                global_online_list.insert(0, current_char)

                # Check for new death
                await self.check_death(current_char.name)
            except asyncio.CancelledError:
                # Task was cancelled, so this is fine
                break
            except Exception as e:
                log.exception("Task: scan_deaths")
                continue

    async def scan_highscores(self):
        #################################################
        #             Nezune's cave                     #
        # Do not touch anything, enter at your own risk #
        #################################################
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            if len(tracked_worlds_list) == 0:
                # If no worlds are tracked, just sleep, worlds might get registered later
                await asyncio.sleep(config.highscores_delay)
                continue
            for world in tracked_worlds_list:
                try:
                    for category in HIGHSCORE_CATEGORIES:
                        # Check the last scan time, highscores are updated every server save
                        with closing(userDatabase.cursor()) as c:
                            c.execute("SELECT last_scan FROM highscores_times WHERE world = ? and category = ?",
                                      (world, category,))
                            result = c.fetchone()
                        if result:
                            last_scan = result["last_scan"]
                            last_scan_date = dt.datetime.utcfromtimestamp(last_scan).replace(tzinfo=dt.timezone.utc)
                            now = dt.datetime.now(dt.timezone.utc)
                            # Current day's server save, could be in the past or the future, an extra hour is added
                            # as margin
                            today_ss = dt.datetime.now(dt.timezone.utc).replace(hour=11 - get_tibia_time_zone())
                            if not now > today_ss > last_scan_date:
                                continue
                        highscore_data = []
                        for pagenum in range(1, 13):
                            # Special cases (ek/rp mls)
                            if category == "magic_ek":
                                scores = await get_highscores(world, "magic", pagenum, 1)
                            elif category == "magic_rp":
                                scores = await get_highscores(world, "magic", pagenum, 2)
                            else:
                                scores = await get_highscores(world, category, pagenum)
                            if scores == ERROR_NETWORK:
                                continue
                            for entry in scores:
                                highscore_data.append(
                                    (entry["rank"], category, world, entry["name"], entry["vocation"], entry["value"]))
                            await asyncio.sleep(config.highscores_page_delay)
                        with userDatabase as conn:
                            # Delete old records
                            conn.execute("DELETE FROM highscores WHERE category = ? AND world = ?", (category, world,))
                            # Add current entries
                            conn.executemany("INSERT INTO highscores(rank, category, world, name, vocation, value) "
                                             "VALUES (?, ?, ?, ?, ?, ?)", highscore_data)
                            # These two executes are equal to an UPDATE OR INSERT
                            conn.execute("UPDATE highscores_times SET last_scan = ? WHERE world = ? AND category = ?",
                                         (time.time(), world, category))
                            conn.execute("INSERT INTO highscores_times(world, last_scan, category) SELECT ?,?,? WHERE "
                                         "(SELECT Changes() = 0)", (world, time.time(), category))
                except asyncio.CancelledError:
                    # Task was cancelled, so this is fine
                    break
                except Exception as e:
                    log.exception("Task: scan_highscores")
                    continue
                await asyncio.sleep(10)

    async def scan_online_chars(self):
        #################################################
        #             Nezune's cave                     #
        # Do not touch anything, enter at your own risk #
        #################################################
        await self.bot.wait_until_ready()
        try:
            with open("data/online_list.dat", "rb") as f:
                saved_list, timestamp = pickle.load(f)
                if (time.time() - timestamp) < config.online_list_expiration:
                    global_online_list.clear()
                    global_online_list.extend(saved_list)
                    log.info("Loaded cached online list")
                else:
                    log.info("Cached online list is too old, discarding")
        except (ValueError, FileNotFoundError, pickle.PickleError):
            log.info("Couldn't fetch cached online list.")
            pass
        while not self.bot.is_closed():
            # Open connection to users.db
            c = userDatabase.cursor()
            try:
                # Pop last server in queue, reinsert it at the beginning
                current_world = tibia_worlds.pop()
                tibia_worlds.insert(0, current_world)

                if current_world.capitalize() not in tracked_worlds_list:
                    await asyncio.sleep(0.1)
                    continue

                await asyncio.sleep(config.online_scan_interval)
                # Get online list for this server
                world = await get_world(current_world)
                if world is None:
                    await asyncio.sleep(0.1)
                    continue
                current_world_online = world.players_online
                if len(current_world_online) == 0:
                    await asyncio.sleep(0.1)
                    continue
                # Save the online list in file
                with open("data/online_list.dat", "wb") as f:
                    pickle.dump((global_online_list, time.time()), f, protocol=pickle.HIGHEST_PROTOCOL)
                # Remove chars that are no longer online from the global_online_list
                offline_list = []
                for char in global_online_list:
                    if char.world not in tibia_worlds:
                        # Remove chars from worlds that no longer exist
                        offline_list.append(char)
                    elif char.world == current_world:
                        offline = True
                        for server_char in current_world_online:
                            if server_char.name == char.name:
                                offline = False
                                break
                        if offline:
                            offline_list.append(char)
                for offline_char in offline_list:
                    global_online_list.remove(offline_char)
                    # Check for deaths and level ups when removing from online list
                    try:
                        name = offline_char.name
                        offline_char = await get_character(name)
                    except NetworkError:
                        log.error(f"scan_online_chars: Could not fetch {name}, NetWorkError")
                        continue
                    if offline_char is not None:
                        c.execute("SELECT name, level, id FROM chars WHERE name LIKE ?", (offline_char.name,))
                        result = c.fetchone()
                        if result:
                            c.execute("UPDATE chars SET level = ? WHERE name LIKE ?",
                                      (offline_char.level, offline_char.name))
                            if offline_char.level > result["level"] > 0:
                                # Saving level up date in database
                                c.execute(
                                    "INSERT INTO char_levelups (char_id,level,date) VALUES(?,?,?)",
                                    (result["id"], offline_char.level, time.time(),)
                                )
                                # Announce the level up
                                await self.announce_level(offline_char.level, char=offline_char)
                        await self.check_death(offline_char.name)
                # Add new online chars and announce level differences
                for server_char in current_world_online:
                    c.execute("SELECT name, level, id, user_id FROM chars WHERE name LIKE ?",
                              (server_char.name,))
                    result = c.fetchone()
                    # If its a stalked character
                    if result:
                        # We update their last level in the db
                        c.execute(
                            "UPDATE chars SET level = ? WHERE name LIKE ?",
                            (server_char.level, server_char.name)
                        )
                        if server_char not in global_online_list:
                            # If the character wasn't in the globalOnlineList we add them
                            # (We insert them at the beginning of the list to avoid messing with the death checks order)
                            global_online_list.insert(0, server_char)
                            await self.check_death(server_char.name)
                        # Else we check for levelup
                        elif server_char.level > result["level"] > 0:
                            # Saving level up date in database
                            c.execute(
                                "INSERT INTO char_levelups (char_id,level,date) VALUES(?,?,?)",
                                (result["id"], server_char.level, time.time(),)
                            )
                            # Announce the level up
                            await self.announce_level(server_char.level, char_name=server_char.name)
                # Watched List checking
                # Iterate through servers with tracked world to find one that matches the current world
                for server, world in tracked_worlds.items():
                    if world == current_world:
                        watched_channel_id = get_server_property("watched_channel", server, is_int=True)
                        if watched_channel_id is None:
                            # This server doesn't have watch list enabled
                            continue
                        watched_channel = self.bot.get_channel(watched_channel_id)  # type: discord.abc.Messageable
                        if watched_channel is None:
                            # This server's watched channel is not available to the bot anymore.
                            continue
                        # Get watched list
                        c.execute("SELECT * FROM watched_list WHERE server_id = ?", (server,))
                        results = c.fetchall()
                        if not results:
                            # List is empty
                            continue
                        # Online watched characters
                        currently_online = []
                        # Watched guilds
                        guild_online = dict()
                        for watched in results:
                            if watched["is_guild"]:
                                try:
                                    guild = await get_guild(watched["name"])
                                    # Todo: Remove deleted guilds from list to avoid unnecessary checks, notify
                                    if guild is None:
                                        continue
                                except NetworkError:
                                    continue
                                # If there's at least one member online, add guild to list
                                if len(guild.online):
                                    guild_online[guild.name] = guild.online
                            # If it is a character, check if he's in the online list
                            for online_char in current_world_online:
                                if online_char.name == watched["name"]:
                                    # Add to online list
                                    currently_online.append(online_char)
                        watched_message_id = get_server_property("watched_message", server, is_int=True)
                        # We try to get the watched message, if the bot can't find it, we just create a new one
                        # This may be because the old message was deleted or this is the first time the list is checked
                        try:
                            watched_message = await watched_channel.get_message(watched_message_id)
                        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                            watched_message = None
                        items = [f"\t{x.name} - Level {x.level} {get_voc_emoji(x.vocation)}" for x in currently_online]
                        onlinecount = len(items)
                        if len(items) > 0 or len(guild_online.keys()) > 0:
                            content = "These watched characters are online:\n"
                            content += "\n".join(items)
                            for guild, members in guild_online.items():
                                content += f"\nGuild: **{guild}**\n"
                                content += "\n".join(
                                    [f"\t{x['name']} - Level {x['level']} {get_voc_emoji(x['vocation'])}"
                                     for x in members])
                                onlinecount+= len(members)
                        else:
                            content = "There are no watched characters online."
                        # Send new watched message or edit last one
                        try:
                            if watched_message is None:
                                new_watched_message = await watched_channel.send(content)
                                set_server_property("watched_message", server, new_watched_message.id)
                            else:
                                await watched_message.edit(content=content)
                            await watched_channel.edit(name=watched_channel.name.split("·",1)[0]+"·"+str(onlinecount))
                        except discord.HTTPException:
                            pass
            except asyncio.CancelledError:
                # Task was cancelled, so this is fine
                break
            except Exception as e:
                log.exception("scan_online_chars")
                continue
            finally:
                userDatabase.commit()
                c.close()

    async def check_death(self, character):
        """Checks if the player has new deaths"""
        try:
            char = await get_character(character)
            if char is None:
                # During server save, characters can't be read sometimes
                return
        except NetworkError:
            log.warning("check_death: couldn't fetch {0}".format(character))
            return
        c = userDatabase.cursor()
        c.execute("SELECT name, id FROM chars WHERE name LIKE ?", (character,))
        result = c.fetchone()
        if result is None:
            return
        char_id = result["id"]
        pending_deaths = []
        for death in char.deaths:
            death_time = death.time.timestamp()
            # Check if we have a death that matches the time
            c.execute("SELECT * FROM char_deaths "
                      "WHERE char_id = ? AND date >= ? AND date <= ? AND level = ? AND killer LIKE ?",
                      (char_id, death_time - 20, death_time + 20, death.level, death.killer))
            result = c.fetchone()
            if result is not None:
                # We already have this death, we're assuming we already have older deaths
                break
            pending_deaths.append(death)
        c.close()

        # Announce and save deaths from older to new
        for death in reversed(pending_deaths):
            with userDatabase as con:
                con.execute("INSERT INTO char_deaths(char_id, level, killer, byplayer, date) VALUES(?,?,?,?,?)",
                            (char_id, death.level, death.killer, death.by_player, death.time.timestamp()))
            if time.time() - death.time.timestamp() >= (30 * 60):
                log.info("Death detected, too old to announce: {0}({1.level}) | {1.killer}".format(character, death))
            else:
                await self.announce_death(death, max(death.level - char.level, 0), char)

    async def announce_death(self, death: Death, levels_lost=0, char: Character = None, char_name: str = None):
        """Announces a level up on the corresponding servers"""
        # Don't announce for low level players
        if int(death.level) < config.announce_threshold:
            return
        if char is None:
            if char_name is None:
                log.error("announce_death: no character or character name passed.")
                return
            try:
                char = await get_character(char_name)
            except NetworkError:
                log.warning("announce_death: couldn't fetch character (" + char_name + ")")
                return

        log.info("Announcing death: {0.name}({1.level}) | {1.killer}".format(char, death))

        # Find killer article (a/an)
        killer_article = ""
        if not death.by_player:
            killer_article = death.killer.split(" ", 1)
            if killer_article[0] in ["a", "an"] and len(killer_article) > 1:
                death.killer = killer_article[1]
                killer_article = killer_article[0] + " "
            else:
                killer_article = ""

        # Select a message
        if death.by_player:
            message = weighed_choice(death_messages_player, vocation=char.vocation, level=death.level,
                                     levels_lost=levels_lost)
        elif death.killer in ["death","energy","earth","fire","Pit Battler","Pit Berserker","Pit Blackling","Pit Brawler","Pit Condemned","Pit Demon","Pit Destroyer","Pit Fiend","Pit Groveller","Pit Grunt","Pit Lord","Pit Maimer","Pit Overlord","Pit Reaver","Pit Scourge"] and levels_lost == 0:
            #skip element damage deaths unless player lost a level to avoid spam from arena deaths
            #this will cause a small amount of deaths to not be announced but it's probably worth the tradeoff (ty selken)
            return
        else:
            message = weighed_choice(death_messages_monster, vocation=char.vocation, level=death.level,
                                     levels_lost=levels_lost, killer=death.killer)
        # Format message with death information
        death_info = {'name': char.name, 'level': death.level, 'killer': death.killer, 'killer_article': killer_article,
                      'he_she': char.he_she.lower(), 'his_her': char.his_her.lower(), 'him_her': char.him_her.lower()}
        message = message.format(**death_info)
        # Format extra stylization
        message = format_message(message)
        if death.by_player:
            message = EMOJI[":skull:"] + " " + message
        else:
            message = EMOJI[":skull_crossbones:"] + " " + message

        for guild_id, tracked_world in tracked_worlds.items():
            guild = self.bot.get_guild(guild_id)
            if char.world == tracked_world and guild is not None and guild.get_member(char.owner) is not None:
                try:
                    channel = self.bot.get_channel_or_top(guild, get_server_property("levels_channel", guild.id,
                                                                                     is_int=True))
                    await channel.send(message[:1].upper() + message[1:])
                except discord.Forbidden:
                    log.warning("announce_death: Missing permissions.")
                except discord.HTTPException:
                    log.warning("announce_death: Malformed message.")

    async def announce_level(self, level, char_name: str = None, char: Character = None):
        """Announces a level up on corresponding servers

        One of these must be passed:
        char is a character dictionary
        char_name is a character's name

        If char_name is passed, the character is fetched here."""
        # Don't announce low level players
        if int(level) < config.announce_threshold:
            return
        if char is None:
            if char_name is None:
                log.error("announce_level: no character or character name passed.")
                return
            try:
                char = await get_character(char_name)
            except NetworkError:
                log.warning("announce_level: couldn't fetch character (" + char_name + ")")
                return

        log.info("Announcing level up: {0} ({1})".format(char.name, level))

        # Select a message
        message = weighed_choice(level_messages, vocation=char.vocation, level=level)
        level_info = {'name': char.name, 'level': level, 'he_she': char.he_she.lower(), 'his_her': char.his_her.lower(),
                      'him_her': char.him_her.lower()}
        # Format message with level information
        message = message.format(**level_info)
        # Format extra stylization
        message = format_message(message)
        message = EMOJI[":star2:"] + " " + message

        for server_id, tracked_world in tracked_worlds.items():
            server = self.bot.get_guild(server_id)
            if char.world == tracked_world and server is not None and server.get_member(char.owner) is not None:
                try:
                    channel = self.bot.get_channel_or_top(server, get_server_property("levels_channel", server.id,
                                                                                      is_int=True))
                    await channel.send(message)
                except discord.Forbidden:
                    log.warning("announce_level: Missing permissions.")
                except discord.HTTPException:
                    log.warning("announce_level: Malformed message.")

    @checks.is_not_lite()
    @commands.command(aliases=["i'm", "iam"])
    async def im(self, ctx, *, char_name: str):
        """Lets you add your tibia character(s) for the bot to track.

        If there are other visible characters, the bot will ask for confirmation to add them too."""
        # This is equivalent to someone using /stalk addacc on themselves.

        user = ctx.author
        # List of servers the user shares with the bot
        user_guilds = self.bot.get_user_guilds(user.id)
        # List of Tibia worlds tracked in the servers the user is
        user_tibia_worlds = [world for guild, world in tracked_worlds.items() if guild in [g.id for g in user_guilds]]
        # Remove duplicate entries from list
        user_tibia_worlds = list(set(user_tibia_worlds))

        if not is_private(ctx.channel) and tracked_worlds.get(ctx.guild.id) is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        if len(user_tibia_worlds) == 0:
            return

        await ctx.trigger_typing()
        try:
            char = await get_character(char_name)
            if char is None:
                await ctx.send("That character doesn't exists.")
                return
        except NetworkError:
            await ctx.send("I couldn't fetch the character, please try again.")
            return
        chars = char.other_characters
        check_other = False
        if len(chars) > 1:
            message = await ctx.send("Do you want to attempt to add the other visible characters in this account?")
            check_other = await self.bot.wait_for_confirmation_reaction(ctx, message, timeout=60)
        if not check_other:
            if check_other is None:
                await ctx.send("Going to take that as a no... Moving on...")
            chars = [char]

        skipped = []
        updated = []
        added = []  # type: List[Character]
        existent = []
        for char in chars:
            # Skip chars in non-tracked worlds
            if char.world not in user_tibia_worlds:
                skipped.append(char)
                continue
            with closing(userDatabase.cursor()) as c:
                c.execute("SELECT name, guild, user_id as owner, vocation, ABS(level) as level, guild FROM chars "
                          "WHERE name LIKE ?", (char.name,))
                db_char = c.fetchone()
            if db_char is not None:
                owner = self.bot.get_member(db_char["owner"])
                # Previous owner doesn't exist anymore
                if owner is None:
                    updated.append({'name': char.name, 'world': char.world, 'prevowner': db_char["owner"],
                                   'vocation': db_char["vocation"], 'level': db_char['level'], 'guild': db_char['guild']
                                    })
                    continue
                # Char already registered to this user
                elif owner.id == user.id:
                    existent.append("{0.name} ({0.world})".format(char))
                    continue
                # Character is registered to another user, we stop the whole process
                else:
                    reply = "Sorry, a character in that account ({0}) is already registered to **{1.display_name}** " \
                            "({1.name}#{1.discriminator}). Maybe you made a mistake?\n" \
                            "If that character really belongs to you, try using `/claim {0}`."
                    await ctx.send(reply.format(db_char["name"], owner))
                    return
            # If we only have one char, it already contains full data
            if len(chars) > 1:
                try:
                    await ctx.message.channel.trigger_typing()
                    char = await get_character(char.name)
                except NetworkError:
                    await ctx.send("I'm having network troubles, please try again.")
                    return
            if char.deleted is not None:
                skipped.append(char)
                continue
            added.append(char)

        if len(skipped) == len(chars):
            reply = "Sorry, I couldn't find any characters from the servers I track ({0})."
            await ctx.send(reply.format(join_list(user_tibia_worlds, ", ", " and ")))
            return

        reply = ""
        log_reply = dict().fromkeys([server.id for server in user_guilds], "")
        if len(existent) > 0:
            reply += "\nThe following characters were already registered to you: {0}" \
                .format(join_list(existent, ", ", " and "))

        if len(added) > 0:
            reply += "\nThe following characters were added to your account: {0}" \
                .format(join_list(["{0.name} ({0.world})".format(c) for c in added], ", ", " and "))
            for char in added:
                log.info("Character {0} was assigned to {1.display_name} (ID: {1.id})".format(char.name, user))
                # Announce on server log of each server
                for guild in user_guilds:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(guild.id, None) == char.world:
                        _guild = "No guild" if char.guild is None else char.guild_name
                        voc = get_voc_abb_and_emoji(char.vocation)
                        log_reply[guild.id] += "\n\u2023 {1.name} - Level {1.level} {2} - **{0}**"\
                            .format(_guild, char, voc)

        if len(updated) > 0:
            reply += "\nThe following characters were reassigned to you: {0}" \
                .format(join_list(["{name} ({world})".format(**c) for c in updated], ", ", " and "))
            for char in updated:
                log.info("Character {0} was reassigned to {1.display_name} (ID: {1.id})".format(char['name'], user))
                # Announce on server log of each server
                for guild in user_guilds:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(guild.id, None) == char["world"]:
                        char["voc"] = get_voc_abb_and_emoji(char["vocation"])
                        if char["guild"] is None:
                            char["guild"] = "No guild"
                        log_reply[guild.id] += "\n\u2023 {name} - Level {level} {voc} - **{guild}** (Reassigned)".\
                            format(**char)

        for char in updated:
            with userDatabase as conn:
                conn.execute("UPDATE chars SET user_id = ? WHERE name LIKE ?", (user.id, char['name']))
        for char in added:
            with userDatabase as conn:
                conn.execute("INSERT INTO chars (name,level,vocation,user_id, world, guild) VALUES (?,?,?,?,?,?)",
                             (char.name, char.level * -1, char.vocation, user.id, char.world,
                              char.guild_name)
                             )

        with userDatabase as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", (user.id, user.display_name,))
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (user.display_name, user.id,))

        await ctx.send(reply)
        for server_id, message in log_reply.items():
            if message:
                message = user.mention + " registered the following characters: " + message
                embed = discord.Embed(description=message)
                embed.set_author(name=f"{user.name}#{user.discriminator}", icon_url=get_user_avatar(user))
                embed.timestamp = dt.datetime.utcnow()
                embed.colour = discord.Colour.dark_teal()
                await self.bot.send_log_message(self.bot.get_guild(server_id), embed=embed)

    @checks.is_not_lite()
    @commands.command(aliases=["i'mnot"])
    async def imnot(self, ctx, *, name):
        """Removes a character assigned to you

        All registered level ups and deaths will be lost forever."""
        c = userDatabase.cursor()
        try:
            c.execute("SELECT id, name, ABS(level) as level, user_id, vocation, world "
                      "FROM chars WHERE name LIKE ?", (name,))
            char = c.fetchone()
            if char is None or char["user_id"] == 0:
                await ctx.send("There's no character registered with that name.")
                return
            if char["user_id"] != ctx.message.author.id:
                await ctx.send("The character **{0}** is not registered to you.".format(char["name"]))
                return

            await ctx.send("Are you sure you want to unregister **{name}** ({level} {vocation})? `yes/no`"
                           .format(**char))

            def check(m):
                return m.channel == ctx.channel and m.author == ctx.author

            try:
                reply = await self.bot.wait_for("message", timeout=50.0, check=check)
                if reply.content.lower() not in ["yes", "y"]:
                    await ctx.send("No then? Ok.")
                    return
            except asyncio.TimeoutError:
                await ctx.send("I guess you changed your mind.")
                return

            c.execute("UPDATE chars SET user_id = 0 WHERE id = ?", (char["id"],))
            await ctx.send("**{0}** is no longer registered to you.".format(char["name"]))

            user_servers = [s.id for s in self.bot.get_user_guilds(ctx.message.author.id)]
            for server_id, world in tracked_worlds.items():
                if char["world"] == world and server_id in user_servers:
                    message = "{0} unregistered **{1}**".format(ctx.message.author.mention, char["name"])
                    await self.bot.send_log_message(self.bot.get_guild(server_id), message)
        finally:
            userDatabase.commit()
            c.close()

    @commands.command()
    @checks.is_not_lite()
    async def claim(self, ctx, *, char_name: str=None):
        """Claims a character registered to someone else

        To use this command, you must put a specific code on the character's comment.
        Use it with no arguments to see the code.

        This allows you to register a character to you, no matter if the character is registered to someone else."""
        user = ctx.author
        claim_pattern = re.compile(r"/NB-([^/]+)/")
        user_code = hex(user.id)[2:].upper()

        # List of servers the user shares with the self.bot
        user_guilds = self.bot.get_user_guilds(user.id)
        # List of Tibia worlds tracked in the servers the user is
        user_tibia_worlds = [world for guild, world in tracked_worlds.items() if guild in [g.id for g in user_guilds]]
        # Remove duplicate entries from list
        user_tibia_worlds = list(set(user_tibia_worlds))

        if not is_private(ctx.channel) and tracked_worlds.get(ctx.guild.id) is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        if len(user_tibia_worlds) == 0:
            return

        if char_name is None:
            await ctx.send(f"To use this command, add `/NB-{user_code}/` to the comment of the character you want to"
                           f"claim, and then use `/claim character_name`.")
            return

        await ctx.trigger_typing()
        try:
            char = await get_character(char_name)
            if char is None:
                await ctx.send("That character doesn't exists.")
                return
        except NetworkError:
            await ctx.send("I couldn't fetch the character, please try again.")
            return
        match = claim_pattern.search(char.comment if char.comment is not None else "")
        if not match:
            await ctx.send(f"Couldn't find verification code on character's comment.\n"
                           f"Add `/NB-{user_code}/` to the comment to authenticate.")
            return
        code = match.group(1)
        if code != user_code:
            await ctx.send(f"The verification code on the character's comment doesn't match yours.\n"
                           f"Use `/NB-{user_code}/` to authenticate.")
            return

        chars = char.other_characters
        check_other = False
        if len(chars) > 1:
            message = await ctx.send("Do you want to attempt to add the other visible characters in this account?")
            check_other = await self.bot.wait_for_confirmation_reaction(ctx, message, timeout=60)
        if not check_other:
            if check_other is None:
                await ctx.send("Going to take that as a no... Moving on...")
            chars = [char]

        skipped = []
        updated = []
        added = []  # type: List[Character]
        existent = []
        for char in chars:
            # Skip chars in non-tracked worlds
            if char.world not in user_tibia_worlds:
                skipped.append(char)
                continue
            with closing(userDatabase.cursor()) as c:
                c.execute("SELECT name, guild, user_id as owner, vocation, ABS(level) as level, guild FROM chars "
                          "WHERE name LIKE ?", (char.name,))
                db_char = c.fetchone()
            if db_char is not None:
                owner = self.bot.get_member(db_char["owner"])
                # Char already registered to this user
                if owner.id == user.id:
                    existent.append("{0.name} ({0.world})".format(char))
                    continue
                else:
                    updated.append({'name': char.name, 'world': char.world, 'prevowner': db_char["owner"],
                                    'vocation': db_char["vocation"], 'level': db_char['level'],
                                    'guild': db_char['guild']
                                    })
            # If we only have one char, it already contains full data
            if len(chars) > 1:
                try:
                    await ctx.message.channel.trigger_typing()
                    char = await get_character(char.name)
                except NetworkError:
                    await ctx.send("I'm having network troubles, please try again.")
                    return
            if char.deleted is not None:
                skipped.append(char)
                continue
            added.append(char)

        if len(skipped) == len(chars):
            reply = "Sorry, I couldn't find any characters from the servers I track ({0})."
            await ctx.send(reply.format(join_list(user_tibia_worlds, ", ", " and ")))
            return

        reply = ""
        log_reply = dict().fromkeys([server.id for server in user_guilds], "")
        if len(existent) > 0:
            reply += "\nThe following characters were already registered to you: {0}" \
                .format(join_list(existent, ", ", " and "))

        if len(added) > 0:
            reply += "\nThe following characters were added to your account: {0}" \
                .format(join_list(["{0.name} ({0.world})".format(c) for c in added], ", ", " and "))
            for char in added:
                log.info("Character {0} was assigned to {1.display_name} (ID: {1.id})".format(char.name, user))
                # Announce on server log of each server
                for guild in user_guilds:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(guild.id, None) == char.world:
                        _guild = "No guild" if char.guild is None else char.guild_name
                        voc = get_voc_abb_and_emoji(char.vocation)
                        log_reply[guild.id] += "\n\u2023 {1.name} - Level {1.level} {2} - **{0}**" \
                            .format(_guild, char, voc)

        if len(updated) > 0:
            reply += "\nThe following characters were reassigned to you: {0}" \
                .format(join_list(["{name} ({world})".format(**c) for c in updated], ", ", " and "))
            for char in updated:
                log.info("Character {0} was reassigned to {1.display_name} (ID: {1.id})".format(char['name'], user))
                # Announce on server log of each server
                for guild in user_guilds:
                    # Only announce on worlds where the character's world is tracked
                    if tracked_worlds.get(guild.id, None) == char["world"]:
                        char["voc"] = get_voc_abb_and_emoji(char["vocation"])
                        if char["guild"] is None:
                            char["guild"] = "No guild"
                        log_reply[guild.id] += "\n\u2023 {name} - Level {level} {voc} - **{guild}** (Reassigned)". \
                            format(**char)

        for char in updated:
            with userDatabase as conn:
                conn.execute("UPDATE chars SET user_id = ? WHERE name LIKE ?", (user.id, char['name']))
        for char in added:
            with userDatabase as conn:
                conn.execute("INSERT INTO chars (name,level,vocation,user_id, world, guild) VALUES (?,?,?,?,?,?)",
                             (char.name, char.level * -1, char.vocation, user.id, char.world,
                              char.guild_name)
                             )

        with userDatabase as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, name) VALUES (?, ?)", (user.id, user.display_name,))
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (user.display_name, user.id,))

        await ctx.send(reply)
        for server_id, message in log_reply.items():
            if message:
                message = user.mention + " registered the following characters: " + message
                embed = discord.Embed(description=message)
                embed.set_author(name=f"{user.name}#{user.discriminator}", icon_url=get_user_avatar(user))
                embed.timestamp = dt.datetime.utcnow()
                embed.colour = discord.Colour.dark_teal()
                await self.bot.send_log_message(self.bot.get_guild(server_id), embed=embed)

    @commands.command()
    @checks.is_not_lite()
    async def online(self, ctx, world: str = None):
        """Tells you which users are online on Tibia

        This list gets updated based on Tibia.com online list, so it takes a couple minutes to be updated.

        If used in a server, only characters from users of the server are shown
        If used on PM, and you are on more than one server with different tracked worlds, you need to specify the world"""
        world = world.capitalize() if world is not None else None
        if world is not None and world not in tibia_worlds:
            await ctx.send("That world doesn't exist.")
            return
        if is_private(ctx.channel):
            user_guilds = self.bot.get_user_guilds(ctx.author.id)
            user_worlds = list(set(self.bot.get_user_worlds(ctx.author.id)))
            if len(user_worlds) == 0:
                return
            if len(user_worlds) > 1 and world is None:
                await ctx.send("You're in more than one server with different worlds, repeat the command with one of "
                               f"the following world: {', '.join(user_worlds)}")
                return
            if len(user_worlds) > 1 and world not in user_worlds:
                await ctx.send(f"You're not in any servers that track {world}")
                return
            if len(user_worlds) == 1:
                world = user_worlds[0]
                title = "Users online"
            else:
                title = f"Users online in {world}"
        else:
            user_guilds = [ctx.guild]
            world = tracked_worlds.get(ctx.guild.id)
            title = "Users online"
            if world is None:
                await ctx.send("This server is not tracking any tibia worlds.")
                return

        ask_channel = self.bot.get_channel_by_name(config.ask_channel_name, ctx.message.guild)
        if is_private(ctx.message.channel) or ctx.message.channel == ask_channel:
            per_page = 20
        else:
            per_page = 5
        c = userDatabase.cursor()
        now = dt.datetime.utcnow()
        uptime = (now - start_time).total_seconds()
        count = 0
        entries = []
        vocations = []
        try:
            for char in global_online_list:
                char_world = char.world
                name = char.name
                c.execute("SELECT name, user_id, vocation, ABS(level) as level FROM chars WHERE name LIKE ?",
                          (name,))
                row = c.fetchone()
                if row is None:
                    continue
                if char_world != world:
                    continue
                # Only show members on this server or members visible to author if it's a pm
                owner = self.bot.get_member(row["user_id"], user_guilds)
                if owner is None:
                    continue
                row["owner"] = owner.display_name
                row['emoji'] = get_voc_emoji(row['vocation'])
                vocations.append(row["vocation"])
                row['vocation'] = get_voc_abb(row['vocation'])
                entries.append("{name} (Lvl {level} {vocation}{emoji}, **@{owner}**)".format(**row))
                count += 1

            if count == 0:
                if uptime < 90:
                    await ctx.send("I just started, give me some time to check online lists..." + EMOJI[":clock2:"])
                else:
                    await ctx.send("There is no one online from Discord.")
                return
            pages = VocationPaginator(self.bot, message=ctx.message, entries=entries, per_page=per_page, title=title,
                                      vocations=vocations)
            try:
                await pages.paginate()
            except CannotPaginate as e:
                await ctx.send(e)
        finally:
            c.close()

    @commands.group(invoke_without_command=True, aliases=["watchlist", "hunted", "huntedlist"])
    @checks.is_admin()
    @commands.guild_only()
    async def watched(self, ctx, *, name="watched-list"):
        """Sets the watched list channel for this server

        Creates a new channel with the specified name.
        If no name is specified, the default name "watched-list" is used."""

        watched_channel_id = get_server_property("watched_channel", ctx.guild.id, is_int=True)
        watched_channel = self.bot.get_channel(watched_channel_id)
        
        if "·" in name:
            await ctx.send("Channel name cannot contain the special character **·**")
            return
        
        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        if watched_channel is not None:
            await ctx.send(f"This server already has a watched list channel: {watched_channel.mention}")
            return
        permissions = ctx.message.channel.permissions_for(ctx.me)  # type: discord.Permissions
        if not permissions.manage_channels:
            await ctx.send("I need to have `Manage Channels` permissions to use this command.")
        try:
            overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                ctx.guild.me: discord.PermissionOverwrite(read_messages=True)
            }
            channel = await ctx.guild.create_text_channel(name, overwrites=overwrites)
        except discord.Forbidden:
            await ctx.send("Sorry, I don't have permissions to create channels.")
        except discord.HTTPException:
            await ctx.send("Something went wrong, the channel name you chose is probably invalid.")
        else:
            await ctx.send(f"Channel created successfully: {channel.mention}\n")
            await channel.send("This is where I will post a list of online watched characters."
                               "Right now only **admins** are able to read this.\n"
                               "Edit this channel's permissions to allow the roles you want.\n"
                               "This channel can be renamed freely.\n"
                               "**It is important to not allow anyone to write in here**\n"
                               "*This message can be deleted now.*")
            set_server_property("watched_channel", ctx.guild.id, channel.id)

    @watched.command(name="add", aliases=["addplayer", "addchar"])
    @commands.guild_only()
    @checks.is_admin()
    async def watched_add(self, ctx, *, name=None):
        """Adds a character to the watched list"""
        if name is None:
            await ctx.send("You need to tell me the name of the person you want to add to the list.")
            return

        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        try:
            char = await get_character(name)
            if char is None:
                await ctx.send("I couldn't fetch that character right now, please try again.")
                return
        except NetworkError:
            await ctx.send("There's no character with that name.")
            return

        if char.world != world:
            await ctx.send(f"This character is not in **{world}**.")
            return
        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND name LIKE ? and is_guild = 0",
                      (ctx.guild.id, char.name))
            result = c.fetchone()
            if result is not None:
                await ctx.send("This character is already in the watched list.")
                return

            message = await ctx.send("Do you want to add **{0.name}** (Level {0.level} {0.vocation}) to the "
                                     "watched list? ".format(char))
            confirm = await self.bot.wait_for_confirmation_reaction(ctx, message)
            if confirm is None:
                await ctx.send("You took too long!")
                return
            if not confirm:
                await ctx.send("Ok then, guess you changed your mind.")
                return

            c.execute("INSERT INTO watched_list(name, server_id, is_guild) VALUES(?, ?, 0)",
                      (char.name, ctx.guild.id,))
            await ctx.send("Character added to the watched list.")
        finally:
            userDatabase.commit()
            c.close()

    @watched.command(name="remove", aliases=["removeplayer", "removechar"])
    @commands.guild_only()
    @checks.is_admin()
    async def watched_remove(self, ctx, *, name=None):
        """Removes a character from the watched list"""
        if name is None:
            ctx.send("You need to tell me the name of the person you want to remove from the list.")

        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND name LIKE ? and is_guild = 0",
                      (ctx.guild.id, name))
            result = c.fetchone()
            if result is None:
                await ctx.send("This character is not in the watched list.")
                return

            message = await ctx.send(f"Do you want to remove **{name}** from the watched list?")
            confirm = await self.bot.wait_for_confirmation_reaction(ctx, message)
            if confirm is None:
                await ctx.send("You took too long!")
                return
            if not confirm:
                await ctx.send("Ok then, guess you changed your mind.")
                return

            c.execute("DELETE FROM watched_list WHERE server_id = ? AND name LIKE ? AND is_guild = 0",
                      (ctx.guild.id, name,))
            await ctx.send("Character removed from the watched list.")
        finally:
            userDatabase.commit()
            c.close()

    @watched.command(name="addguild")
    @commands.guild_only()
    @checks.is_admin()
    async def watched_addguild(self, ctx, *, name=None):
        """Adds an entire guild to the watched list
        
        Guilds are displayed in the watched list as a group.
        If a new member joins, he will automatically displayed here,
        on the other hand, if a member leaves, it won't be shown anymore."""
        if name is None:
            ctx.send("You need to tell me the name of the guild you want to add.")
            return

        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        try:
            guild = await get_guild(name)
            if guild is None:
                await ctx.send("There's no guild with that name.")
                return
        except NetworkError:
            await ctx.send("I couldn't fetch that guild right now, please try again.")
            return

        if guild.world != world:
            await ctx.send(f"This guild is not in **{world}**.")
            return
        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND name LIKE ? and is_guild = 1",
                      (ctx.guild.id, guild.name))
            result = c.fetchone()
            if result is not None:
                await ctx.send("This guild is already in the watched list.")
                return

            message = await ctx.send(f"Do you want to add the guild **{guild.name}** to the watched list?")
            confirm = await self.bot.wait_for_confirmation_reaction(ctx, message,
                                                                    "Ok then, guess you changed your mind.")
            if confirm is None:
                await ctx.send("You took too long!")
                return
            if not confirm:
                await ctx.send("Ok then, guess you changed your mind.")
                return

            c.execute("INSERT INTO watched_list(name, server_id, is_guild) VALUES(?, ?, 1)",
                      (guild.name, ctx.guild.id,))
            await ctx.send("Guild added to the watched list.")
        finally:
            userDatabase.commit()
            c.close()

    @watched.command(name="removeguild")
    @commands.guild_only()
    @checks.is_admin()
    async def watched_removeguild(self, ctx, *, name=None):
        """Removes a guild from the watched list"""
        if name is None:
            ctx.send("You need to tell me the name of the guild you want to remove from the list.")

        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return

        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND name LIKE ? and is_guild = 1",
                      (ctx.guild.id, name))
            result = c.fetchone()
            if result is None:
                await ctx.send("This guild is not in the watched list.")
                return

            message = await ctx.send(f"Do you want to remove **{name}** from the watched list?")
            confirm = await self.bot.wait_for_confirmation_reaction(ctx, message)
            if confirm is None:
                await ctx.send("You took too long!")
                return
            if not confirm:
                await ctx.send("Ok then, guess you changed your mind.")
                return

            c.execute("DELETE FROM watched_list WHERE server_id = ? AND name LIKE ? AND is_guild = 1",
                      (ctx.guild.id, name,))
            await ctx.send("Guild removed from the watched list.")
        finally:
            userDatabase.commit()
            c.close()

    @watched.command(name="list")
    @commands.guild_only()
    @checks.is_admin()
    async def watched_list(self, ctx):
        """Shows a list of all watched characters
        
        Note that this lists all characters, not just online characters."""
        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return
        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND is_guild = 0 ORDER BY name ASC",
                      (ctx.guild.id,))
            results = c.fetchall()
            if not results:
                await ctx.send("There are no characters in the watched list.")
                return
            entries = [f"[{r['name']}]({get_character_url(r['name'])})" for r in results]
        finally:
            c.close()
        pages = Paginator(self.bot, message=ctx.message, entries=entries, title="Watched Characters")
        try:
            await pages.paginate()
        except CannotPaginate as e:
            await ctx.send(e)

    @watched.command(name="guildlist", aliases=["listguilds", "guilds"])
    @commands.guild_only()
    @checks.is_admin()
    async def watched_list_guild(self, ctx):
        """Shows a list of all watched characters

        Note that this lists all characters, not just online characters."""
        world = tracked_worlds.get(ctx.guild.id, None)
        if world is None:
            await ctx.send("This server is not tracking any tibia worlds.")
            return
        c = userDatabase.cursor()
        try:
            c.execute("SELECT * FROM watched_list WHERE server_id = ? AND is_guild = 1 ORDER BY name ASC",
                      (ctx.guild.id,))
            results = c.fetchall()
            if not results:
                await ctx.send("There are no guilds in the watched list.")
                return
            entries = [f"[{r['name']}]({url_guild+urllib.parse.quote(r['name'])})" for r in results]
        finally:
            c.close()
        pages = Paginator(self.bot, message=ctx.message, entries=entries, title="Watched Guilds")
        try:
            await pages.paginate()
        except CannotPaginate as e:
            await ctx.send(e)

    def __unload(self):
        print("cogs.tracking: Cancelling pending tasks...")
        self.scan_deaths_task.cancel()
        self.scan_highscores_task.cancel()
        self.scan_online_chars_task.cancel()


def setup(bot):
    bot.add_cog(Tracking(bot))
