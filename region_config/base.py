"""
Abstract base class for region + technology configuration.

Every region/technology combination provides a concrete subclass that supplies
the data constants, prompt fragments, and search parameters used by the agent
modules.  The base class documents the full interface and provides sensible
defaults for truly universal attributes.
"""
from abc import ABC, abstractmethod


class RegionConfig(ABC):
    """Base configuration that every region+technology config must implement."""

    # ── Identity ─────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def region_name(self) -> str:
        """English name of the region (e.g. 'Turkey')."""

    @property
    @abstractmethod
    def region_name_local(self) -> str:
        """Region name in local language (e.g. 'Türkiye')."""

    @property
    @abstractmethod
    def technology_domain(self) -> str:
        """Technology focus (e.g. 'AI', 'Biotech', 'Fintech')."""

    @property
    @abstractmethod
    def language_codes(self) -> list:
        """ISO language codes used for search queries (e.g. ['tr', 'en'])."""

    # ── Language & Text Processing ───────────────────────────────────────────
    @property
    @abstractmethod
    def character_mapping(self) -> dict:
        """Uppercase-to-lowercase mapping for locale-specific characters."""

    @property
    @abstractmethod
    def asciify_map(self) -> dict:
        """Diacritical-to-ASCII fallback mapping for fuzzy matching."""

    @property
    @abstractmethod
    def corporate_suffixes(self) -> list:
        """Legal entity suffixes to strip before fuzzy matching."""

    @property
    @abstractmethod
    def stop_words(self) -> set:
        """Region/domain-specific stop words excluded from keyword learning."""

    # ── News & Search ────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def external_news_sources(self) -> list:
        """Domain names of news sources to monitor."""

    @property
    @abstractmethod
    def news_tech_keywords(self) -> list:
        """Technology-domain keywords in local + English for news quality gate."""

    @property
    @abstractmethod
    def news_action_keywords(self) -> list:
        """Action/event keywords for news quality gate."""

    @property
    @abstractmethod
    def news_negative_keywords(self) -> list:
        """Negative keywords to filter out noise in news."""

    @property
    @abstractmethod
    def news_queries(self) -> list:
        """Structured news search queries: list of {query, source, lang}."""

    @property
    @abstractmethod
    def news_extraction_rules(self) -> str:
        """LLM prompt fragment for region-specific news extraction rules."""

    # ── Ecosystem Search Templates ───────────────────────────────────────────
    @property
    @abstractmethod
    def ecosystem_search_templates(self) -> dict:
        """Per-entity-type search query templates: {type: [template_str, ...]}."""

    # ── Entity Heuristic Keywords ────────────────────────────────────────────
    @property
    @abstractmethod
    def media_keywords(self) -> list:
        """Keywords to heuristically detect Media entities."""

    @property
    @abstractmethod
    def techpark_keywords(self) -> list:
        """Keywords to heuristically detect Techpark/Teknopark entities."""

    @property
    @abstractmethod
    def accelerator_keywords(self) -> list:
        """Keywords to heuristically detect Accelerator entities."""

    @property
    @abstractmethod
    def government_keywords(self) -> list:
        """Keywords to heuristically detect Government entities."""

    # ── Taxonomy ─────────────────────────────────────────────────────────────

    # Universal base matrix (shared across all configs)
    BASE_SECTOR_TAG_MATRIX = {
        ("Manufacturing", "Adv. Manufacturing"),
        ("Manufacturing", "Automotive & Mobility"),
        ("Manufacturing", "Textiles & Apparel"),
        ("Manufacturing", "Robotics & Automation"),
        ("Defense", "Unmanned Systems"),
        ("Defense", "Cybersecurity"),
        ("Finance", "Fintech"),
        ("Finance", "Banking & Insurtech"),
        ("Finance", "Accounting & Tax"),
        ("Health", "Healthtech"),
        ("Public Sector", "GovTech"),
        ("Public Sector", "EdTech"),
        ("Public Sector", "Smart Cities"),
        ("Agri-Food", "Agritech"),
        ("Services", "Logistics & Supply Chain"),
        ("Services", "E-commerce & Retail"),
        ("Services", "HR & Recruitment"),
        ("Services", "Marketing & AdTech"),
        ("Services", "Legal & RegTech"),
        ("Services", "Media & Entertainment"),
        ("Services", "Hospitality & Travel"),
        ("Services", "Customer Experience"),
        ("Services", "Telecom & Connectivity"),
        ("Services", "Gaming & Esports"),
        ("Technology", "AI Infrastructure"),
        ("Technology", "Data & Analytics"),
        ("Technology", "Developer Tools"),
        ("Technology", "Software Development"),
        ("Energy", "CleanTech & Energy"),
        ("Real Estate", "PropTech & Construction"),
        ("General", "Uncategorized"),
    }

    # Universal matrix description string (shared)
    BASE_MATRIX_DESCRIPTION_STR = """Industrial Sector | Tag/Category | Primary AI Use Case Context
Manufacturing | Adv. Manufacturing | Predictive maintenance and Industry 4.0.
Manufacturing | Automotive & Mobility | Autonomous driving and EV management.
Manufacturing | Textiles & Apparel | CV-based defect detection and water saving.
Manufacturing | Robotics & Automation | Industrial robotics, RPA, and warehouse automation.
Defense | Unmanned Systems | Autonomous UAVs, UGVs, and target recognition.
Defense | Cybersecurity | AI-driven threat detection and secure networks.
Finance | Fintech | Credit scoring, fraud detection, and crypto trading.
Finance | Banking & Insurtech | Customer service chatbots and risk management.
Finance | Accounting & Tax | AI bookkeeping, invoicing, expense management, tax automation.
Health | Healthtech | Diagnostic imaging and medical data analytics.
Public Sector | GovTech | Digital government services and tax admin.
Public Sector | EdTech | Personalized learning and teacher support.
Public Sector | Smart Cities | Urban traffic and resource management.
Agri-Food | Agritech | Precision farming and plant disease detection.
Services | Logistics & Supply Chain | Route optimization and warehouse robotics.
Services | E-commerce & Retail | Personalization engines and demand forecasting.
Services | HR & Recruitment | AI-powered talent matching and HR analytics.
Services | Marketing & AdTech | Programmatic advertising and customer insights.
Services | Legal & RegTech | Contract analysis and regulatory compliance.
Services | Media & Entertainment | Content recommendation and media production.
Services | Hospitality & Travel | Hotel, restaurant, tourism, and food service management AI.
Services | Customer Experience | CRM, chatbots, customer support, and contact center AI.
Services | Telecom & Connectivity | Telecom network optimization, 5G, and ISP management.
Services | Gaming & Esports | Game development, game AI, and esports platforms.
Technology | AI Infrastructure | MLOps, model serving, and AI platforms.
Technology | Data & Analytics | Big data processing and business intelligence.
Technology | Developer Tools | IDEs, SDKs, DevOps, CI/CD, code generation, and developer APIs.
Technology | Software Development | Industry-agnostic horizontal software tools.
Energy | CleanTech & Energy | Smart grid management and renewable energy.
Real Estate | PropTech & Construction | Property analytics and construction AI.
General | Uncategorized | Could not determine a specific category.
"""

    @property
    def domain_extensions(self) -> set:
        """Additional sector/tag pairs for this technology domain. Override to extend."""
        return set()

    @property
    def valid_matrix(self) -> set:
        """Combined universal + domain-specific sector/tag pairs."""
        return self.BASE_SECTOR_TAG_MATRIX | self.domain_extensions

    @property
    def matrix_description_str(self) -> str:
        """Full matrix description string for LLM prompts. Override to extend."""
        return self.BASE_MATRIX_DESCRIPTION_STR

    @property
    @abstractmethod
    def tag_seed_keywords(self) -> dict:
        """Keyword seeds per tag category for classification bootstrapping."""

    # ── Geography ────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def location_canonical(self) -> dict:
        """Canonical location mapping for normalization."""

    @property
    @abstractmethod
    def domestic_markers(self) -> list:
        """City/country strings that indicate domestic origin."""

    @property
    @abstractmethod
    def major_cities(self) -> list:
        """Major startup hub city names for validation."""

    @property
    @abstractmethod
    def funding_sanity_cap(self) -> float:
        """Max plausible funding amount (USD) before flagging as hallucination."""

    # ── LLM Prompt Fragments ─────────────────────────────────────────────────
    @property
    @abstractmethod
    def pass_1_context_rules(self) -> str:
        """Region-specific context rules for Pass 1 (fact extraction) prompt."""

    @property
    @abstractmethod
    def pass_2_sector_context(self) -> str:
        """Region-specific sector context for Pass 2 (taxonomy mapping) prompt."""

    @property
    @abstractmethod
    def pass_3_formatting_rules(self) -> str:
        """Region-specific formatting rules for Pass 3 (audit) prompt."""

    @property
    @abstractmethod
    def investor_domestic_instruction(self) -> str:
        """Instruction for identifying domestic companies in investor pass 2."""

    @property
    @abstractmethod
    def investor_character_rules(self) -> str:
        """Character/formatting rules for investor pass 3."""

    @property
    @abstractmethod
    def funding_format_description(self) -> str:
        """Description of the local funding amount format for prompts."""

    # ── Search Query Builders ──────────────────────────────────────────���─────
    @abstractmethod
    def investor_search_queries(self, investor_name: str) -> list:
        """Return list of search query strings for enriching an investor."""

    @abstractmethod
    def startup_search_queries(self, company_name: str) -> list:
        """Return list of search query strings for enriching a startup."""

    # ── Convenience Methods ──────────────────────────────────────────────────
    def normalize_text(self, text: str) -> str:
        """Lowercase text using locale-specific character mapping."""
        if not isinstance(text, str):
            return ""
        for k, v in self.character_mapping.items():
            text = text.replace(k, v)
        return text.lower()

    def asciify(self, text: str) -> str:
        """Replace diacritical characters with ASCII equivalents."""
        for k, v in self.asciify_map.items():
            text = text.replace(k, v)
        return text

    def is_domestic(self, location: str) -> bool:
        """Check if a location string indicates domestic origin."""
        if not location:
            return False
        loc_lower = location.lower()
        return any(marker.lower() in loc_lower for marker in self.domestic_markers)

    def classify_origin(self, location: str) -> str:
        """Classify location as Domestic, International, or Unknown."""
        if not location or str(location).strip().lower() in (
            "", "unknown", "n/a", "none", "global", "europe"
        ):
            return "Unknown"
        if self.is_domestic(location):
            return "Domestic"
        return "International"
