                error_vi = "⚠️ Gemini không trả về phản hồi. Hãy kiểm tra GEMINI_API_KEY, model Gemini hoặc giới hạn API."
            else:
                error_en = "⚠️ Premium AI did not return a response. Check OPENAI_API_KEY, the OpenAI model, or the API quota."
                error_vi = "⚠️ Premium AI không trả về phản hồi. Hãy kiểm tra OPENAI_API_KEY, model OpenAI hoặc giới hạn API."

            await message.channel.send(
                tr(message.channel.id, error_en, error_vi)
            )
        else:
            await send_ai_response(
                message,
                answer
            )

            task = asyncio.create_task(
                learn_from_message(message)
            )

            learning_tasks.setdefault(
                guild_id,
                set()
            )

            learning_tasks[guild_id].add(task)

            task.add_done_callback(
                lambda t: learning_tasks[guild_id].discard(t)
            )

    except Exception as e:
        print("[AI ERROR]", e)

        try:
            await message.channel.send(
                tr(
                    message.channel.id,
                    "⚠️ An error occurred while processing AI.",
                    "⚠️ Có lỗi khi xử lý AI.",
                )
            )
        except Exception:
            pass

    await bot.process_commands(message)


# =========================================================
# COMMAND ERROR HANDLERS
# =========================================================

async def permission_error(interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = tr(
            interaction.channel.id if interaction.channel else 0,
            "❌ You need the Manage Server permission.",
            "❌ Bạn cần quyền Quản lý Server.",
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def generic_app_command_error(interaction, error):
    print("[COMMAND ERROR]", repr(error))
    message = tr(
        interaction.channel.id if interaction.channel else 0,
        "❌ The command could not be completed. Check the arguments and bot permissions.",
        "❌ Không thể thực hiện lệnh. Hãy kiểm tra tham số và quyền của bot.",
    )
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


for _command in (
    status_slash,
    ai_memory,
    language_command,
    gif,
    help_command,
    support_slash,
    premium_slash,
    premium_add_slash,
    premium_remove_slash,
    caption_slash,
    avatar_slash,
    mute_slash,
    ban_slash,
    unmute_slash,
    unban_slash,
):
    _command.error(generic_app_command_error)


ai_on.error(permission_error)
ai_off.error(permission_error)
ai_channel.error(permission_error)
ai_interval.error(permission_error)
genz_add.error(permission_error)
gif_add.error(permission_error)
mute_slash.error(permission_error)
ban_slash.error(permission_error)


# =========================================================
# START
# =========================================================

if not TOKEN:
    print("❌ DISCORD_TOKEN is missing from .env")
else:
    init_db()
    bot.run(TOKEN)