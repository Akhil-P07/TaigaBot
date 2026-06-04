"""AI/ML engagement commands for the club.

  /paper <query>  — search arXiv for recent papers
  /resource [topic] — curated learning resources (edit RESOURCES below)
  /aiterm         — a random "AI term of the day" with a definition

Edit RESOURCES and AI_TERMS freely — they're plain Python data.
"""
from __future__ import annotations

import random
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

ARXIV_API = "http://export.arxiv.org/api/query"

# ✏️ Curated resources — add your own club favorites here.
RESOURCES: dict[str, list[tuple[str, str]]] = {
    "Getting started": [
        ("3Blue1Brown — Neural Networks", "https://www.youtube.com/playlist?list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi"),
        ("fast.ai — Practical Deep Learning", "https://course.fast.ai/"),
        ("Andrej Karpathy — Zero to Hero", "https://karpathy.ai/zero-to-hero.html"),
    ],
    "Math & foundations": [
        ("MIT 18.06 Linear Algebra", "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/"),
        ("StatQuest", "https://www.youtube.com/c/joshstarmer"),
    ],
    "LLMs & NLP": [
        ("Hugging Face Course", "https://huggingface.co/learn/nlp-course"),
        ("Stanford CS224N", "https://web.stanford.edu/class/cs224n/"),
    ],
    "Tools": [
        ("PyTorch Tutorials", "https://pytorch.org/tutorials/"),
        ("Papers With Code", "https://paperswithcode.com/"),
        ("Weights & Biases", "https://wandb.ai/site"),
    ],
}

# ✏️ AI term of the day pool.
AI_TERMS: list[tuple[str, str]] = [
    ("Overfitting", "When a model learns the training data too well — including its noise — and fails to generalize to new data."),
    ("Gradient Descent", "An optimization algorithm that iteratively adjusts parameters in the direction that reduces the loss."),
    ("Transformer", "A neural network architecture built on self-attention; the basis of modern LLMs."),
    ("Embedding", "A dense vector representation of data (words, images, etc.) that captures semantic meaning."),
    ("Backpropagation", "The algorithm that computes gradients of the loss w.r.t. each weight by applying the chain rule backward through the network."),
    ("Attention", "A mechanism letting a model weigh the importance of different parts of the input when producing each output."),
    ("Hallucination", "When a generative model produces fluent but factually incorrect or fabricated output."),
    ("Fine-tuning", "Continuing training of a pretrained model on a smaller, task-specific dataset."),
    ("RAG", "Retrieval-Augmented Generation: grounding an LLM's answers in documents fetched at query time."),
    ("Regularization", "Techniques (like dropout or weight decay) that discourage overly complex models to improve generalization."),
    ("Tokenization", "Splitting text into smaller units (tokens) that a model processes."),
    ("Epoch", "One full pass of the learning algorithm over the entire training dataset."),
]


class Resources(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="paper", description="Search arXiv for AI/ML papers.")
    @app_commands.describe(query="Search terms, e.g. 'diffusion models'")
    async def paper(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": 5,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        url = f"{ARXIV_API}?{urllib.parse.urlencode(params)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    text = await resp.text()
        except Exception:  # noqa: BLE001
            await interaction.followup.send("⚠️ Couldn't reach arXiv right now. Try again later.")
            return

        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(text)
        entries = root.findall("a:entry", ns)
        if not entries:
            await interaction.followup.send(f"No papers found for **{query}**.")
            return

        embed = discord.Embed(
            title=f"📄 arXiv results for “{query}”", color=config.BOT_COLOR
        )
        for e in entries:
            title = " ".join(e.find("a:title", ns).text.split())
            link = e.find("a:id", ns).text
            authors = [a.find("a:name", ns).text for a in e.findall("a:author", ns)]
            author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
            summary = " ".join(e.find("a:summary", ns).text.split())
            embed.add_field(
                name=title[:240],
                value=f"*{author_str}*\n{summary[:200]}…\n[Read it]({link})",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="resource", description="Curated AI/ML learning resources.")
    @app_commands.describe(topic="Optional topic")
    async def resource(self, interaction: discord.Interaction, topic: str | None = None):
        embed = discord.Embed(title="📚 AI Club resources", color=config.BOT_COLOR)
        items = RESOURCES.items()
        if topic:
            t = topic.lower()
            items = [(k, v) for k, v in RESOURCES.items() if t in k.lower()] or RESOURCES.items()
        for category, links in items:
            embed.add_field(
                name=category,
                value="\n".join(f"• [{name}]({url})" for name, url in links),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="aiterm", description="Learn an AI term of the day.")
    async def aiterm(self, interaction: discord.Interaction):
        term, definition = random.choice(AI_TERMS)
        embed = discord.Embed(
            title=f"🧠 AI Term: {term}", description=definition, color=config.BOT_COLOR
        )
        embed.set_footer(text="Run /aiterm again for another!")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Resources(bot))
