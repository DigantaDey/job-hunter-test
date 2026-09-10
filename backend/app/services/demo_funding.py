"""
Demo funding dataset.

Synthetic by construction and therefore *only* used when
``ALLOW_SYNTHETIC_FUNDING_DATA=true``: the product never presents invented
companies as real funding events (blockers #3 in the launch assessment).
Real data comes from ``app.services.funding_sources`` (SEC EDGAR Form D,
Crunchbase/Tracxn when keys are present, or an operator-supplied export).
"""
from typing import Any, Dict, List

CURATED_COMPANIES: List[Dict[str, Any]] = [
    {
        "name": "VectorLoom AI",
        "website": "vectorloom.ai",
        "industry": "ai/ml",
        "stage": "Series A"
    },
    {
        "name": "PromptForge",
        "website": "promptforge.dev",
        "industry": "ai/ml",
        "stage": "Seed"
    },
    {
        "name": "Cortexa Labs",
        "website": "cortexa.ai",
        "industry": "ai/ml",
        "stage": "Series B"
    },
    {
        "name": "Lumen Intelligence",
        "website": "lumenint.ai",
        "industry": "ai/ml",
        "stage": "Series C"
    },
    {
        "name": "Synthetiq",
        "website": "synthetiq.ai",
        "industry": "ai/ml",
        "stage": "Series A"
    },
    {
        "name": "Payflow Systems",
        "website": "payflow.io",
        "industry": "fintech",
        "stage": "Series B"
    },
    {
        "name": "Ledgerline",
        "website": "ledgerline.com",
        "industry": "fintech",
        "stage": "Series A"
    },
    {
        "name": "Kredit Works",
        "website": "kreditworks.com",
        "industry": "fintech",
        "stage": "Seed"
    },
    {
        "name": "Vaultpay",
        "website": "vaultpay.co",
        "industry": "fintech",
        "stage": "Series C"
    },
    {
        "name": "Insurely AI",
        "website": "insurely.ai",
        "industry": "fintech",
        "stage": "Series A"
    },
    {
        "name": "Tradegrail",
        "website": "tradegrail.com",
        "industry": "fintech",
        "stage": "Series D"
    },
    {
        "name": "Shopstack",
        "website": "shopstack.io",
        "industry": "e-commerce",
        "stage": "Series B"
    },
    {
        "name": "Cartfull",
        "website": "cartfull.com",
        "industry": "e-commerce",
        "stage": "Series A"
    },
    {
        "name": "Mercato Live",
        "website": "mercatolive.com",
        "industry": "e-commerce",
        "stage": "Seed"
    },
    {
        "name": "Medlane Health",
        "website": "medlane.health",
        "industry": "healthtech",
        "stage": "Series B"
    },
    {
        "name": "Careloop",
        "website": "careloop.health",
        "industry": "healthtech",
        "stage": "Series A"
    },
    {
        "name": "Genomix Bio",
        "website": "genomix.bio",
        "industry": "healthtech",
        "stage": "Series C"
    },
    {
        "name": "Learnloop",
        "website": "learnloop.app",
        "industry": "edtech",
        "stage": "Series A"
    },
    {
        "name": "Skillforge Academy",
        "website": "skillforge.academy",
        "industry": "edtech",
        "stage": "Seed"
    },
    {
        "name": "Devkit Cloud",
        "website": "devkit.cloud",
        "industry": "developer tools",
        "stage": "Series A"
    },
    {
        "name": "Pipeline HQ",
        "website": "pipelinehq.dev",
        "industry": "developer tools",
        "stage": "Seed"
    },
    {
        "name": "MergeBase",
        "website": "mergebase.dev",
        "industry": "developer tools",
        "stage": "Series B"
    },
    {
        "name": "Shipped CI",
        "website": "shipped.ci",
        "industry": "developer tools",
        "stage": "Series A"
    },
    {
        "name": "Aegis Security",
        "website": "aegis.security",
        "industry": "cybersecurity",
        "stage": "Series B"
    },
    {
        "name": "ZeroTrace",
        "website": "zerotrace.io",
        "industry": "cybersecurity",
        "stage": "Series A"
    },
    {
        "name": "Flowdesk SaaS",
        "website": "flowdesk.com",
        "industry": "saas",
        "stage": "Series C"
    },
    {
        "name": "Taskgrid",
        "website": "taskgrid.app",
        "industry": "saas",
        "stage": "Series A"
    },
    {
        "name": "Opsly",
        "website": "opsly.io",
        "industry": "saas",
        "stage": "Seed"
    },
    {
        "name": "Wovenwork",
        "website": "wovenwork.com",
        "industry": "saas",
        "stage": "Series B"
    },
    {
        "name": "Relay CRM",
        "website": "relaycrm.com",
        "industry": "saas",
        "stage": "Series D"
    },
    {
        "name": "Playcraft Studios",
        "website": "playcraft.gg",
        "industry": "gaming",
        "stage": "Series A"
    },
    {
        "name": "Nova Arcade",
        "website": "novaarcade.com",
        "industry": "gaming",
        "stage": "Seed"
    },
    {
        "name": "Chainforge Labs",
        "website": "chainforge.xyz",
        "industry": "web3",
        "stage": "Series A"
    },
    {
        "name": "DeFi Bridge",
        "website": "defibridge.finance",
        "industry": "web3",
        "stage": "Series B"
    },
    {
        "name": "Fleetly Mobility",
        "website": "fleetly.mobility",
        "industry": "mobility",
        "stage": "Series B"
    },
    {
        "name": "CargoStream",
        "website": "cargostream.io",
        "industry": "mobility",
        "stage": "Series A"
    },
    {
        "name": "VoltaRide",
        "website": "voltaride.com",
        "industry": "mobility",
        "stage": "Series C"
    },
    {
        "name": "Helio Grid",
        "website": "heliogrid.energy",
        "industry": "climate",
        "stage": "Series A"
    },
    {
        "name": "Carbonloop",
        "website": "carbonloop.earth",
        "industry": "climate",
        "stage": "Seed"
    },
    {
        "name": "Datacraft Analytics",
        "website": "datacraft.io",
        "industry": "data",
        "stage": "Series B"
    },
    {
        "name": "Warehouse OS",
        "website": "warehouseos.io",
        "industry": "data",
        "stage": "Series A"
    },
    {
        "name": "Streamline ETL",
        "website": "streamline-etl.com",
        "industry": "data",
        "stage": "Seed"
    },
    {
        "name": "Cloudframe",
        "website": "cloudframe.io",
        "industry": "developer tools",
        "stage": "Series C"
    },
    {
        "name": "Nimbus Compute",
        "website": "nimbuscompute.com",
        "industry": "developer tools",
        "stage": "Series B"
    },
    {
        "name": "Querybird",
        "website": "querybird.com",
        "industry": "data",
        "stage": "Series A"
    },
    {
        "name": "Modelhub AI",
        "website": "modelhub.ai",
        "industry": "ai/ml",
        "stage": "Series D"
    },
    {
        "name": "Agentive",
        "website": "agentive.ai",
        "industry": "ai/ml",
        "stage": "Seed"
    }
]
