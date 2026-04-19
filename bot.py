import discord
from discord import app_commands
from discord.ext import commands
from supabase import create_client
from dotenv import load_dotenv
import os
from datetime import datetime as dt, date

load_dotenv()

# ── CLIENTS ──────────────────────────────────────────────────────────────────

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── HELPERS ───────────────────────────────────────────────────────────────────

def get_player_team(discord_id: str, server_id: str):
    """Returns (team_id, team_name) or (None, None) if not on a team."""
    result = supabase.table("players")\
        .select("*, teams(*)")\
        .eq("discord_id", discord_id)\
        .execute()

    if not result.data or not result.data[0].get("team_id"):
        return None, None

    team = result.data[0]["teams"]
    if team["server_id"] != server_id:
        return None, None

    return result.data[0]["team_id"], team["team_name"]


def parse_time(time_str: str):
    """Convert 9pm / 9:30pm to display string and db timestamp."""
    time_str = time_str.strip()
    for fmt in ("%I%p", "%I:%M%p", "%H:%M", "%I %p", "%I:%M %p"):
        try:
            parsed = dt.strptime(time_str.upper(), fmt)
            display = parsed.strftime("%I:%M %p")
            db_ts   = dt.combine(date.today(), parsed.time()).isoformat()
            return display, db_ts
        except ValueError:
            continue
    return time_str, dt.now().isoformat()

# ── EVENTS ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user} — slash commands synced")


@bot.event
async def on_reaction_add(reaction, user):
    """Handles ✅ reactions for both 3v3 LFS and cashout LFS posts."""
    if user.bot:
        return
    if str(reaction.emoji) != "✅":
        return

    message = await reaction.message.channel.fetch_message(reaction.message.id)
    if not message.embeds:
        return

    embed = message.embeds[0]
    footer_text = embed.footer.text if embed.footer else ""

    if "LFS" not in footer_text:
        return

    server_id  = str(message.guild.id)
    discord_id = str(user.id)

    accepting_team_id, accepting_team_name = get_player_team(discord_id, server_id)
    if not accepting_team_id:
        await message.channel.send(
            f"❌ {user.mention} you need to be on a team to accept. Use `/join_team` first.",
            delete_after=10
        )
        return

    # ── CASHOUT MODE (4 teams) ───────────────────────────────────────────────
    if footer_text == "CASHOUT_LFS":
        # Parse current teams from embed description
        desc         = embed.description or ""
        filled_teams = [t.strip() for t in desc.split("\n") if t.strip().startswith("•")]
        team_names   = [t.lstrip("• ").strip() for t in filled_teams]

        if accepting_team_name in team_names:
            return  # already joined

        team_names.append(accepting_team_name)
        slots_filled = len(team_names)
        time_field   = next((f.value for f in embed.fields if f.name == "Time"),   "TBD")
        blocks_field = next((f.value for f in embed.fields if f.name == "Blocks"), "1")
        host_name    = embed.title.replace(" are LFS 💰", "").strip()

        if slots_filled < 4:
            # Update embed with new slot count
            teams_str   = "\n".join([f"• {t}" for t in team_names])
            updated_embed = discord.Embed(
                title=f"{host_name} are LFS 💰",
                description=f"**Slots: {slots_filled}/4**\n{teams_str}",
                color=0xffaa00
            )
            updated_embed.add_field(name="Time",         value=time_field,   inline=True)
            updated_embed.add_field(name="Blocks",       value=blocks_field, inline=True)
            updated_embed.add_field(name="How to join",  value="React ✅ to claim a slot!", inline=False)
            updated_embed.set_footer(text="CASHOUT_LFS")
            await message.edit(embed=updated_embed)

        else:
            # All 4 slots filled — confirm!
            filled_embed = discord.Embed(
                title=embed.title,
                description="✅ **Lobby full!**",
                color=0x555555
            )
            await message.edit(embed=filled_embed)

            teams_str = "\n".join([f"• {t}" for t in team_names])
            confirm_embed = discord.Embed(
                title="💰 Cashout Scrim Confirmed!",
                color=0x00ff88
            )
            confirm_embed.add_field(name="Teams",  value=teams_str,    inline=False)
            confirm_embed.add_field(name="Time",   value=time_field,   inline=True)
            confirm_embed.add_field(name="Blocks", value=blocks_field, inline=True)
            confirm_embed.set_footer(text="GL HF 🎮")
            await message.channel.send(embed=confirm_embed)

    # ── 3v3 MODE (2 teams) ───────────────────────────────────────────────────
    elif footer_text == "LFS":
        posting_team_name = embed.title.replace(" are LFS 📢", "").strip()
        if accepting_team_name == posting_team_name:
            return

        time_field   = next((f.value for f in embed.fields if f.name == "Time"),   "TBD")
        blocks_field = next((f.value for f in embed.fields if f.name == "Blocks"), "1")

        posting_team = supabase.table("teams")\
            .select("*")\
            .eq("server_id", server_id)\
            .eq("team_name", posting_team_name)\
            .execute()

        if posting_team.data:
            team_id      = posting_team.data[0]["id"]
            # Update the original scrim with the opponent name
            original = supabase.table("scrims")\
                .update({"opponent": accepting_team_name})\
                .eq("team_id", team_id)\
                .eq("opponent", "OPEN")\
                .execute()

            # Get the scrim details to mirror for the accepting team
            scrim_data = supabase.table("scrims")\
                .select("*")\
                .eq("team_id", team_id)\
                .eq("opponent", accepting_team_name)\
                .order("created_at", desc=True)\
                .limit(1)\
                .execute()

            # Create a mirrored scrim for the accepting team so they can /gg too
            if scrim_data.data and accepting_team_id:
                s = scrim_data.data[0]
                supabase.table("scrims").insert({
                    "team_id":      accepting_team_id,
                    "opponent":     posting_team_name,
                    "scheduled_at": s["scheduled_at"],
                    "map":          s["map"],
                    "notes":        s["notes"]
                }).execute()

        filled_embed = discord.Embed(
            title=embed.title,
            description="✅ **Scrim filled!**",
            color=0x555555
        )
        await message.edit(embed=filled_embed)

        confirm_embed = discord.Embed(title="🎮 Scrim Confirmed!", color=0x00ff88)
        confirm_embed.add_field(name="Teams",  value=f"**{posting_team_name}** vs **{accepting_team_name}**", inline=False)
        confirm_embed.add_field(name="Time",   value=time_field,   inline=True)
        confirm_embed.add_field(name="Blocks", value=blocks_field, inline=True)
        confirm_embed.set_footer(text="GL HF 🎮")
        await message.channel.send(embed=confirm_embed)

# ── TEAM COMMANDS ─────────────────────────────────────────────────────────────

@tree.command(name="create_team", description="Create a new team for your server")
@app_commands.describe(team_name="Name of the team to create")
async def create_team(interaction: discord.Interaction, team_name: str):
    server_id = str(interaction.guild_id)

    existing = supabase.table("teams")\
        .select("*")\
        .eq("server_id", server_id)\
        .eq("team_name", team_name)\
        .execute()

    if existing.data:
        await interaction.response.send_message(
            f"❌ **{team_name}** already exists. Use `/join_team` to join.", ephemeral=True
        )
        return

    supabase.table("teams").insert({
        "server_id": server_id,
        "team_name": team_name
    }).execute()

    await interaction.response.send_message(
        f"✅ Team **{team_name}** created! Use `/join_team` to join."
    )


@tree.command(name="join_team", description="Join an existing team")
@app_commands.describe(team_name="Name of the team to join")
async def join_team(interaction: discord.Interaction, team_name: str):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)
    username   = str(interaction.user.name)

    team = supabase.table("teams")\
        .select("*")\
        .eq("server_id", server_id)\
        .eq("team_name", team_name)\
        .execute()

    if not team.data:
        await interaction.response.send_message(
            f"❌ No team named **{team_name}**. Use `/create_team` first.", ephemeral=True
        )
        return

    team_id = team.data[0]["id"]

    existing = supabase.table("players")\
        .select("*")\
        .eq("discord_id", discord_id)\
        .execute()

    if existing.data:
        supabase.table("players")\
            .update({"team_id": team_id, "discord_username": username})\
            .eq("discord_id", discord_id)\
            .execute()
    else:
        supabase.table("players").insert({
            "discord_id": discord_id,
            "discord_username": username,
            "team_id": team_id
        }).execute()

    await interaction.response.send_message(f"✅ **{username}** joined **{team_name}**!")


@tree.command(name="roster", description="See a team's players")
@app_commands.describe(team_name="Team name (leave blank for your own team)")
async def roster(interaction: discord.Interaction, team_name: str = None):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    if not team_name:
        _, team_name = get_player_team(discord_id, server_id)
        if not team_name:
            await interaction.response.send_message(
                "❌ You're not on a team. Use `/join_team` first.", ephemeral=True
            )
            return

    team = supabase.table("teams")\
        .select("*")\
        .eq("server_id", server_id)\
        .eq("team_name", team_name)\
        .execute()

    if not team.data:
        await interaction.response.send_message(f"❌ No team named **{team_name}**.", ephemeral=True)
        return

    players = supabase.table("players")\
        .select("*")\
        .eq("team_id", team.data[0]["id"])\
        .execute()

    if not players.data:
        await interaction.response.send_message(f"**{team_name}** has no players yet.")
        return

    names = "\n".join([f"• {p['discord_username']}" for p in players.data])
    embed = discord.Embed(title=f"🎮 {team_name} Roster", description=names, color=0x00ff88)
    await interaction.response.send_message(embed=embed)

# ── CORE: LFS ─────────────────────────────────────────────────────────────────

@tree.command(name="lfs", description="Post a Looking For Scrim")
@app_commands.describe(
    time="What time? (e.g. 8pm, 9:30pm)",
    blocks="How many blocks? (default 1 — each block = 1 full map rotation)"
)
async def lfs(interaction: discord.Interaction, time: str, blocks: int = 1):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message(
            "❌ You're not on a team yet. Use `/join_team` first.", ephemeral=True
        )
        return

    display_time, db_timestamp = parse_time(time)
    block_text = f"{blocks} block{'s' if blocks > 1 else ''} ({blocks} full map rotation{'s' if blocks > 1 else ''})"

    supabase.table("scrims").insert({
        "team_id":      team_id,
        "opponent":     "OPEN",
        "scheduled_at": db_timestamp,
        "map":          block_text,
        "notes":        f"{blocks} blocks"
    }).execute()

    embed = discord.Embed(title=f"{team_name} are LFS 📢", color=0x00ff88)
    embed.add_field(name="Time",         value=display_time, inline=True)
    embed.add_field(name="Blocks",       value=block_text,   inline=True)
    embed.add_field(name="How to accept", value="React ✅ below to lock in this scrim!", inline=False)
    embed.set_footer(text="LFS")

    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("✅")

    # Save message ID so /cancel can edit it later
    supabase.table("scrims")\
        .update({"message_id": str(msg.id), "channel_id": str(msg.channel.id)})\
        .eq("team_id", team_id)\
        .eq("opponent", "OPEN")\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()

# ── RESULT LOGGING ────────────────────────────────────────────────────────────

@tree.command(name="gg", description="Log your scrim result — only you will see the confirmation")
@app_commands.describe(
    outcome="Did you win, lose, or draw?",
    score="Score (e.g. 2-1)",
    notes="Any notes about the scrim"
)
@app_commands.choices(outcome=[
    app_commands.Choice(name="Win",  value="win"),
    app_commands.Choice(name="Loss", value="loss"),
    app_commands.Choice(name="Draw", value="draw"),
])
async def gg(interaction: discord.Interaction, outcome: str, score: str = "N/A", notes: str = ""):
    discord_id = str(interaction.user.id)
    server_id  = str(interaction.guild_id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message("❌ You're not on a team yet.", ephemeral=True)
        return

    scrim = supabase.table("scrims")\
        .select("*")\
        .eq("team_id", team_id)\
        .neq("opponent", "OPEN")\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()

    if not scrim.data:
        await interaction.response.send_message(
            "❌ No completed scrims found for your team.", ephemeral=True
        )
        return

    scrim_id = scrim.data[0]["id"]
    opponent = scrim.data[0]["opponent"]

    # Parse individual wins/losses from score like "4-2"
    ind_wins = ind_losses = 0
    if score and "-" in score:
        try:
            parts = score.split("-")
            ind_wins   = int(parts[0].strip())
            ind_losses = int(parts[1].strip())
        except (ValueError, IndexError):
            pass

    supabase.table("results").insert({
        "scrim_id":          scrim_id,
        "outcome":           outcome,
        "score":             score,
        "notes":             notes,
        "individual_wins":   ind_wins,
        "individual_losses": ind_losses,
    }).execute()

    colors = {"win": 0x00ff88, "loss": 0xff4444, "draw": 0xffaa00}
    icons  = {"win": "🏆",     "loss": "💀",      "draw": "🤝"}

    embed = discord.Embed(title=f"{icons[outcome]} GG!", color=colors[outcome])
    embed.add_field(name="Team",     value=team_name,       inline=True)
    embed.add_field(name="Opponent", value=opponent,        inline=True)
    embed.add_field(name="Result",   value=outcome.upper(), inline=True)
    embed.add_field(name="Score",    value=score,           inline=True)
    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)

    # Ephemeral — only the user who ran /gg sees this
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── RECORD ────────────────────────────────────────────────────────────────────

@tree.command(name="record", description="See your team's win/loss record — only visible to you")
@app_commands.describe(team_name="Team name (leave blank for your own team)")
async def record(interaction: discord.Interaction, team_name: str = None):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    if team_name:
        team = supabase.table("teams")\
            .select("*")\
            .eq("server_id", server_id)\
            .eq("team_name", team_name)\
            .execute()
        if not team.data:
            await interaction.response.send_message(
                f"❌ No team named **{team_name}**.", ephemeral=True
            )
            return
        team_id   = team.data[0]["id"]
        team_name = team.data[0]["team_name"]
    else:
        team_id, team_name = get_player_team(discord_id, server_id)
        if not team_id:
            await interaction.response.send_message(
                "❌ You're not on a team.", ephemeral=True
            )
            return

    scrims = supabase.table("scrims")\
        .select("id")\
        .eq("team_id", team_id)\
        .neq("opponent", "OPEN")\
        .execute()

    if not scrims.data:
        await interaction.response.send_message(
            f"**{team_name}** has no scrims logged yet.", ephemeral=True
        )
        return

    scrim_ids = [s["id"] for s in scrims.data]
    results   = supabase.table("results")\
        .select("*")\
        .in_("scrim_id", scrim_ids)\
        .execute()

    wins    = sum(1 for r in results.data if r["outcome"] == "win")
    losses  = sum(1 for r in results.data if r["outcome"] == "loss")
    draws   = sum(1 for r in results.data if r["outcome"] == "draw")
    total   = wins + losses + draws
    winrate = f"{wins/total*100:.0f}%" if total > 0 else "N/A"

    map_wins   = sum(r.get("individual_wins",   0) or 0 for r in results.data)
    map_losses = sum(r.get("individual_losses", 0) or 0 for r in results.data)
    map_total  = map_wins + map_losses
    map_wr     = f"{map_wins/map_total*100:.0f}%" if map_total > 0 else "N/A"

    embed = discord.Embed(title=f"📊 {team_name} Record", color=0x00ff88)
    embed.add_field(name="── Block Record ──", value="\u200b", inline=False)
    embed.add_field(name="Wins",      value=str(wins),   inline=True)
    embed.add_field(name="Losses",    value=str(losses), inline=True)
    embed.add_field(name="Win Rate",  value=winrate,     inline=True)
    embed.add_field(name="── Map Record ──",  value="\u200b", inline=False)
    embed.add_field(name="Map Wins",   value=str(map_wins),   inline=True)
    embed.add_field(name="Map Losses", value=str(map_losses), inline=True)
    embed.add_field(name="Map Win %",  value=map_wr,          inline=True)

    # Ephemeral — only the user who ran /record sees this
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── CASHOUT LFS ───────────────────────────────────────────────────────────────

@tree.command(name="lfs_cashout", description="Post a Looking For Scrim for cashout mode (3v3v3v3 — needs 4 teams)")
@app_commands.describe(
    time="What time? (e.g. 8pm, 9:30pm)",
    blocks="How many blocks? (default 1)"
)
async def lfs_cashout(interaction: discord.Interaction, time: str, blocks: int = 1):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message(
            "❌ You're not on a team yet. Use `/join_team` first.", ephemeral=True
        )
        return

    display_time, db_timestamp = parse_time(time)
    block_text = f"{blocks} block{'s' if blocks > 1 else ''} ({blocks} full map rotation{'s' if blocks > 1 else ''})"

    supabase.table("scrims").insert({
        "team_id":      team_id,
        "opponent":     "CASHOUT_OPEN",
        "scheduled_at": db_timestamp,
        "map":          block_text,
        "notes":        f"cashout {blocks} blocks"
    }).execute()

    embed = discord.Embed(
        title=f"{team_name} are LFS 💰",
        description=f"**Slots: 1/4**\n• {team_name}",
        color=0xffaa00
    )
    embed.add_field(name="Time",        value=display_time, inline=True)
    embed.add_field(name="Blocks",      value=block_text,   inline=True)
    embed.add_field(name="How to join", value="React ✅ to claim a slot! Need 3 more teams.", inline=False)
    embed.set_footer(text="CASHOUT_LFS")

    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("✅")

    # Save message ID so /cancel can edit it later
    supabase.table("scrims")\
        .update({"message_id": str(msg.id), "channel_id": str(msg.channel.id)})\
        .eq("team_id", team_id)\
        .eq("opponent", "CASHOUT_OPEN")\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()

# ── LEAVE TEAM ───────────────────────────────────────────────────────────────

@tree.command(name="leave_team", description="Leave your current team")
async def leave_team(interaction: discord.Interaction):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message("❌ You're not on a team.", ephemeral=True)
        return

    supabase.table("players")\
        .update({"team_id": None})\
        .eq("discord_id", discord_id)\
        .execute()

    await interaction.response.send_message(f"✅ You left **{team_name}**.", ephemeral=True)


# ── RENAME TEAM ───────────────────────────────────────────────────────────────

@tree.command(name="rename_team", description="Rename your team — record is preserved")
@app_commands.describe(new_name="New team name")
async def rename_team(interaction: discord.Interaction, new_name: str):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, old_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message("❌ You're not on a team.", ephemeral=True)
        return

    # Check name isn't already taken
    existing = supabase.table("teams")\
        .select("*")\
        .eq("server_id", server_id)\
        .eq("team_name", new_name)\
        .execute()

    if existing.data:
        await interaction.response.send_message(
            f"❌ **{new_name}** is already taken.", ephemeral=True
        )
        return

    supabase.table("teams")\
        .update({"team_name": new_name})\
        .eq("id", team_id)\
        .execute()

    await interaction.response.send_message(
        f"✅ Team renamed from **{old_name}** to **{new_name}**. Record preserved!"
    )


# ── CANCEL LFS ────────────────────────────────────────────────────────────────

@tree.command(name="cancel", description="Cancel your team's open LFS post")
async def cancel(interaction: discord.Interaction):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message("❌ You're not on a team.", ephemeral=True)
        return

    # Find open scrim
    open_scrim = supabase.table("scrims")\
        .select("*")\
        .eq("team_id", team_id)\
        .in_("opponent", ["OPEN", "CASHOUT_OPEN"])\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()

    if not open_scrim.data:
        await interaction.response.send_message(
            f"❌ No open LFS found for **{team_name}**.", ephemeral=True
        )
        return

    # Delete from DB and edit the original message
    scrim_id   = open_scrim.data[0]["id"]
    message_id = open_scrim.data[0].get("message_id")
    channel_id = open_scrim.data[0].get("channel_id")

    supabase.table("scrims").delete().eq("id", scrim_id).execute()

    # Edit the original embed to show cancelled
    if message_id and channel_id:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                msg = await channel.fetch_message(int(message_id))
                cancelled_embed = discord.Embed(
                    title=f"{team_name} — LFS Cancelled ❌",
                    description="This scrim has been cancelled.",
                    color=0xff4444
                )
                await msg.edit(embed=cancelled_embed)
        except Exception:
            pass  # message already deleted or not found

    await interaction.response.send_message(
        f"✅ **{team_name}**'s open LFS has been cancelled.", ephemeral=True
    )


# ── HISTORY ───────────────────────────────────────────────────────────────────

@tree.command(name="history", description="See your team's last 5 scrims — only visible to you")
async def history(interaction: discord.Interaction):
    server_id  = str(interaction.guild_id)
    discord_id = str(interaction.user.id)

    team_id, team_name = get_player_team(discord_id, server_id)
    if not team_id:
        await interaction.response.send_message("❌ You're not on a team.", ephemeral=True)
        return

    scrims = supabase.table("scrims")\
        .select("*")\
        .eq("team_id", team_id)\
        .neq("opponent", "OPEN")\
        .neq("opponent", "CASHOUT_OPEN")\
        .order("created_at", desc=True)\
        .limit(5)\
        .execute()

    if not scrims.data:
        await interaction.response.send_message(
            f"**{team_name}** has no scrim history yet.", ephemeral=True
        )
        return

    embed = discord.Embed(title=f"📋 {team_name} — Last 5 Scrims", color=0x00ff88)

    for s in scrims.data:
        # Get result for this scrim
        result = supabase.table("results")\
            .select("*")\
            .eq("scrim_id", s["id"])\
            .execute()

        if result.data:
            r       = result.data[0]
            outcome = r["outcome"].upper()
            score   = r["score"] if r["score"] != "N/A" else ""
            icons   = {"WIN": "🏆", "LOSS": "💀", "DRAW": "🤝"}
            icon    = icons.get(outcome, "")
            value   = f"{icon} {outcome} {score}".strip()
        else:
            value = "⏳ No result logged"

        embed.add_field(
            name=f"vs {s['opponent']}",
            value=value,
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── TEAMS ─────────────────────────────────────────────────────────────────────

@tree.command(name="teams", description="List all teams registered in this server")
async def teams(interaction: discord.Interaction):
    server_id = str(interaction.guild_id)

    all_teams = supabase.table("teams")\
        .select("*")\
        .eq("server_id", server_id)\
        .execute()

    if not all_teams.data:
        await interaction.response.send_message(
            "No teams yet. Use `/create_team` to make one!", ephemeral=True
        )
        return

    names = "\n".join([f"• {t['team_name']}" for t in all_teams.data])
    embed = discord.Embed(
        title="🏆 Registered Teams",
        description=names,
        color=0x00ff88
    )
    await interaction.response.send_message(embed=embed)

# ── RUN ───────────────────────────────────────────────────────────────────────

bot.run(os.getenv("DISCORD_TOKEN"))