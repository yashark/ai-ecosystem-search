"""
Turkey + AI region configuration.

All Turkey-specific and AI-specific constants, keywords, prompt fragments,
and search parameters extracted from agent modules.
"""
from region_config.base import RegionConfig


class TurkeyAIConfig(RegionConfig):
    """Configuration for Turkish AI ecosystem tracking."""

    # ── Identity ─────────────────────────────────────────────────────────────
    @property
    def region_name(self):
        return "Turkey"

    @property
    def region_name_local(self):
        return "Türkiye"

    @property
    def technology_domain(self):
        return "AI"

    @property
    def language_codes(self):
        return ["tr", "en"]

    # ── Language & Text Processing ───────────────────────────────────────────
    @property
    def character_mapping(self):
        return {
            'I': 'ı',
            'İ': 'i',
            'Ş': 'ş',
            'Ğ': 'ğ',
            'Ü': 'ü',
            'Ö': 'ö',
            'Ç': 'ç',
        }

    @property
    def asciify_map(self):
        return {
            'ı': 'i', 'i': 'i', 'ş': 's', 'ğ': 'g',
            'ü': 'u', 'ö': 'o', 'ç': 'c',
        }

    @property
    def corporate_suffixes(self):
        return [
            ' a.ş.', ' a.s.', ' ltd.', ' ltd.şti.', ' ltd.sti.',
            ' teknoloji', ' yazılım', ' yazilim', ' holding',
            ' bilişim', ' bilisim', ' danışmanlık', ' danismanlik',
        ]

    @property
    def stop_words(self):
        return {
            'the', 'a', 'an', 'and', 'or', 'is', 'are', 'was', 'were', 'in', 'on', 'at',
            'to', 'for', 'of', 'with', 'by', 'from', 'as', 'it', 'its', 'that', 'this',
            'be', 'has', 'have', 'had', 'not', 'but', 'if', 'we', 'our', 'they', 'their',
            'can', 'will', 'do', 'does', 'also', 'such', 'using', 'based', 'which', 'into',
            'more', 'than', 'been', 'being', 'both', 'each', 'most', 'some', 'about',
            'company', 'startup', 'turkish', 'turkey', 'istanbul', 'ankara', 'provides',
            'offers', 'solution', 'solutions', 'service', 'services', 'technology',
            'technologies', 'innovative', 'leverages', 'cutting-edge', 'leading',
            'various', 'industries', 'businesses', 'users', 'enable', 'enables',
            'artificial', 'intelligence', 'machine', 'learning', 'powered', 'driven',
            'utilizes', 'help', 'helps', 'develop', 'develops', 'development',
            'specializes', 'focus', 'focused', 'designed', 'create', 'creating',
        }

    # ── News & Search ────────────────────────────────────────────────────────
    @property
    def external_news_sources(self):
        return [
            'webrazzi.com', 'egrisim.com', 'shiftdelete.net', 'techseu.com',
            'haberturk.com', 'ekonomim.com', 'bloomberght.com', 'dunya.com',
            'reuters.com', 'bloomberg.com', 'techcrunch.com', 'venturebeat.com',
            'milliyet.com.tr', 'sabah.com.tr', 'hurriyet.com.tr',
            'bigpara.com', 'borsagundem.com',
        ]

    @property
    def news_tech_keywords(self):
        return [
            "yapay zeka", "yapayzeka", "makine öğrenimi", "derin öğrenme",
            "üretken yapay zeka", "gen ai", "llm", "robotik", "otonom", "bilgisayarlı görü",
            "nlp", "computer vision", "deep learning",
        ]

    @property
    def news_action_keywords(self):
        return [
            # Funding / investment
            "yatırım", "yatirim", "funding", "raised", "round", "seed", "series", "milyon", "milyar",
            "investment",
            # Partnerships / acquisitions
            "partnership", "ortaklık", "satın alma", "acquisition",
            # Grants / support programs
            "hibe", "grant", "support",
            # Ecosystem events/programs
            "program", "accelerator", "incubator", "hızlandırıcı", "kuluçka", "demo day",
            # Launch / announcement
            "launch", "launched", "açıklad", "announcement", "announced", "etkinlik",
        ]

    @property
    def news_negative_keywords(self):
        return ["hr", "insan kaynakları", "iş ilanı", "kariyer", "personel"]

    @property
    def news_queries(self):
        return [
            # Turkish Sources
            {"query": "webrazzi yapay zeka OR makine öğrenimi startup yatırım Türkiye 2025", "source": "Webrazzi", "lang": "tr"},
            {"query": "egirisim yapay zeka OR üretken yapay zeka OR derin öğrenme girişim 2025", "source": "Egirişim", "lang": "tr"},
            {"query": "bloomberght yapay zeka OR otonom OR robotik girişim yatırım 2025", "source": "Bloomberg HT", "lang": "tr"},
            {"query": "webrazzi OR egirisim deeptech OR derin teknoloji startup Türkiye 2025", "source": "Deeptech TR", "lang": "tr"},
            {"query": "dunya.com OR hürriyet yapay zeka startup yatırım teknoloji 2025", "source": "Hürriyet/Dünya", "lang": "tr"},
            {"query": "sabah.com.tr OR anadolu ajansı yapay zeka girişim yatırım 2025", "source": "Sabah/AA", "lang": "tr"},
            # English Sources
            {"query": "daily sabah AI OR machine learning startup Turkey investment 2025", "source": "Daily Sabah", "lang": "en"},
            {"query": "sifted.eu Turkey AI OR deeptech startup funding 2024 2025", "source": "Sifted", "lang": "en"},
            {"query": "fintechnews AI OR fintech Turkey startup 2024 2025", "source": "Fintechnews ME", "lang": "en"},
            {"query": "techcrunch OR venturebeat Turkey AI startup investment 2025", "source": "Intl Tech Press", "lang": "en"},
            # Broad Turkish
            {"query": "(yapay zeka OR makine öğrenimi OR derin öğrenme OR bilgisayarlı görü OR doğal dil işleme) startup Türkiye yatırım 2024 2025", "source": "General", "lang": "tr"},
            {"query": "(üretken yapay zeka OR büyük dil modeli OR LLM OR ajantik OR deeptech OR derin teknoloji) girişim Türkiye 2024 2025", "source": "General", "lang": "tr"},
            {"query": "(otonom OR robotik OR yapay sinir ağı OR büyük veri) startup Türkiye yatırım", "source": "General", "lang": "tr"},
            # Broad English
            {"query": "(AI OR machine learning OR deep learning OR computer vision OR NLP OR generative AI OR Gen AI) startup Turkey investment 2024 2025", "source": "General", "lang": "en"},
            {"query": "(agentic AI OR deeptech OR autonomous OR robotics OR edge AI OR IoT) startup Turkey funding 2024 2025", "source": "General", "lang": "en"},
            {"query": "Turkey AI startup funding round seed series 2025", "source": "General", "lang": "en"},
        ]

    @property
    def news_extraction_rules(self):
        return """TURKISH EXTRACTION RULES:
- Company names: preserve exact Turkish suffixes (A.Ş., Ltd.Şti., Teknoloji). Use proper Turkish characters.
- Funding amounts: Turkish uses period for thousands (e.g., "5.000.000 TL" = 5 million TL). Convert to standard format.
- "milyon" = million, "milyar" = billion. "yatırım aldı" = received investment. "değerleme" = valuation.
- Common Turkish investor types: melek yatırımcı (angel), girişim sermayesi (VC), kamu fonu (government fund).
- Date: Turkish months — Ocak, Şubat, Mart, Nisan, Mayıs, Haziran, Temmuz, Ağustos, Eylül, Ekim, Kasım, Aralık.

IMPORTANT: Only extract companies that are BASED IN or OPERATING FROM Turkey.
Do NOT extract:
- Global tech companies (Google, Microsoft, OpenAI, Meta, Amazon, Nvidia, Apple, xAI, Anthropic, Databricks, etc.)
- Companies mentioned only as partners, customers, or technology providers
- Companies mentioned only in comparisons ("like Uber but for...")
If the article says "Turkish startup X partnered with Google", extract ONLY "X", not "Google"."""

    # ── Ecosystem Search Templates ───────────────────────────────────────────
    @property
    def ecosystem_search_templates(self):
        return {
            'Corporate': [
                '"{name}" yapay zeka OR AI OR dijital dönüşüm yatırım OR ortaklık',
                '"{name}" artificial intelligence OR machine learning investment OR partnership',
                '"{name}" startup yatırım OR satın alma OR işbirliği teknoloji',
            ],
            'Teknopark': [
                '"{name}" yapay zeka girişim OR startup program',
                '"{name}" AI incubator OR accelerator batch cohort',
                '"{name}" resident startups OR kiracı şirketler teknoloji',
            ],
            'Accelerator': [
                '"{name}" batch OR cohort OR demo day AI startup',
                '"{name}" portfolio şirket OR yatırım OR program',
            ],
            'Government': [
                '"{name}" yapay zeka strateji OR hibe OR destek programı',
                '"{name}" AI OR dijital dönüşüm regulation OR grant OR funding',
            ],
            'Media': [
                '"{name}" summit OR hackathon OR demo day OR etkinlik düzenledi',
                '"{name}" event OR konferans OR zirve organize',
            ],
            'Other': [
                '"{name}" yapay zeka OR AI OR dijital dönüşüm yatırım OR ortaklık',
                '"{name}" artificial intelligence OR machine learning investment OR partnership',
                '"{name}" startup yatırım OR satın alma OR işbirliği teknoloji',
            ],
            'NGO': [
                '"{name}" yapay zeka strateji OR hibe OR destek programı',
                '"{name}" AI OR dijital dönüşüm regulation OR grant OR funding',
            ],
        }

    # ── Entity Heuristic Keywords ────────────────────────────────────────────
    @property
    def media_keywords(self):
        return ['webrazzi', 'haber', 'dergi', 'blog', 'magazine', 'news', 'media', 'gazete', 'yayın']

    @property
    def techpark_keywords(self):
        return ['teknokent', 'teknopark', 'technopark', 'cyberpark', 'teknoloji geliştirme bölgesi']

    @property
    def accelerator_keywords(self):
        return ['accelerator', 'incubator', 'hızlandırıcı', 'kuluçka']

    @property
    def government_keywords(self):
        return ['bakanlık', 'ministry', 'tübitak', 'kosgeb', 'tubitak']

    # ── Taxonomy ─────────────────────────────────────────────────────────────
    @property
    def tag_seed_keywords(self):
        return {
            'Adv. Manufacturing': ['manufacturing', 'factory', 'industrial', 'production', 'assembly', 'industry 4.0', 'quality control'],
            'Automotive & Mobility': ['automotive', 'vehicle', 'autonomous driving', 'mobility', 'electric vehicle', 'fleet'],
            'Textiles & Apparel': ['textile', 'apparel', 'fashion', 'garment', 'fabric', 'defect detection'],
            'Unmanned Systems': ['drone', 'uav', 'ugv', 'unmanned', 'autonomous vehicle', 'reconnaissance'],
            'Cybersecurity': ['cybersecurity', 'security', 'threat detection', 'firewall', 'vulnerability', 'penetration'],
            'Fintech': ['fintech', 'payment', 'credit', 'fraud', 'banking', 'lending', 'financial'],
            'Banking & Insurtech': ['insurance', 'insurtech', 'banking', 'risk management', 'underwriting'],
            'Healthtech': ['health', 'medical', 'clinical', 'diagnostic', 'patient', 'hospital', 'pharma', 'biotech'],
            'GovTech': ['government', 'public sector', 'municipality', 'civic', 'tax', 'e-government'],
            'EdTech': ['education', 'learning', 'student', 'teacher', 'school', 'course', 'training'],
            'Smart Cities': ['smart city', 'urban', 'traffic management', 'city planning', 'infrastructure'],
            'Agritech': ['agriculture', 'farming', 'crop', 'irrigation', 'livestock', 'soil', 'harvest'],
            'Logistics & Supply Chain': ['logistics', 'supply chain', 'shipping', 'delivery', 'warehouse', 'fleet', 'cargo', 'freight', 'transport', 'courier', 'tracking shipment'],
            'E-commerce & Retail': ['e-commerce', 'ecommerce', 'retail', 'shop', 'marketplace', 'merchant', 'consumer', 'cart', 'store'],
            'HR & Recruitment': ['recruitment', 'hiring', 'talent', 'human resources', 'resume', 'candidate', 'workforce'],
            'Marketing & AdTech': ['marketing', 'advertising', 'adtech', 'campaign', 'programmatic', 'customer engagement', 'crm'],
            'Legal & RegTech': ['legal', 'compliance', 'regulatory', 'contract', 'regtech', 'law'],
            'Media & Entertainment': ['media', 'entertainment', 'content', 'video', 'streaming', 'gaming', 'music'],
            'AI Infrastructure': ['mlops', 'model serving', 'inference', 'gpu', 'training pipeline', 'ai platform', 'foundation model', 'llm'],
            'Data & Analytics': ['analytics', 'big data', 'data warehouse', 'business intelligence', 'visualization', 'data pipeline'],
            'Software Development': ['software development', 'custom software', 'software house', 'app development', 'digital transformation', 'it solutions', 'software consulting', 'web development', 'generic platform', 'horizontal software', 'multi-tenant', 'no-code', 'low-code', 'workflow automation', 'project management', 'productivity'],
            'Robotics & Automation': ['robotics', 'robot', 'rpa', 'robotic process automation', 'industrial robot', 'cobot', 'warehouse automation', 'pick and place'],
            'Accounting & Tax': ['accounting', 'bookkeeping', 'invoicing', 'expense', 'tax', 'payroll', 'financial reporting', 'audit'],
            'Hospitality & Travel': ['hotel', 'restaurant', 'tourism', 'travel', 'hospitality', 'food service', 'booking', 'reservation', 'cafe', 'coffee'],
            'Customer Experience': ['crm', 'customer support', 'chatbot', 'contact center', 'call center', 'helpdesk', 'customer service', 'customer experience', 'cx', 'ticket'],
            'Telecom & Connectivity': ['telecom', 'telecommunication', '5g', 'network', 'isp', 'connectivity', 'fiber', 'mobile operator'],
            'Gaming & Esports': ['game', 'gaming', 'esports', 'game development', 'game engine', 'game ai', 'metaverse', 'virtual world'],
            'Developer Tools': ['ide', 'sdk', 'devops', 'ci/cd', 'code generation', 'developer api', 'developer tools', 'git', 'testing framework', 'debugging', 'compiler'],
            'CleanTech & Energy': ['energy', 'solar', 'wind', 'renewable', 'smart grid', 'carbon', 'sustainability', 'cleantech'],
            'PropTech & Construction': ['real estate', 'property', 'construction', 'building', 'proptech', 'architecture'],
            'Uncategorized': [],
        }

    # ── Geography ────────────────────────────────────────────────────────────
    @property
    def location_canonical(self):
        return {
            # Turkish city fixes (proper İ)
            "Istanbul": "İstanbul",
            "Istanbul, Turkey": "İstanbul, Turkey",
            "Istanbul, Türkiye": "İstanbul, Turkey",
            "Izmir": "İzmir",
            "Izmir, Turkey": "İzmir, Turkey",
            "Isparta": "İsparta",
            "Iskenderun": "İskenderun",
            # Country standardization
            "USA": "United States",
            "US": "United States",
            "U.S.": "United States",
            "U.S.A.": "United States",
            "UK": "United Kingdom",
            "U.K.": "United Kingdom",
            "Türkiye": "Turkey",
            # City + Country standardization
            "San Francisco, USA": "San Francisco, United States",
            "San Francisco, California": "San Francisco, United States",
            "San Francisco, CA": "San Francisco, United States",
            "New York, USA": "New York, United States",
            "New York, NY": "New York, United States",
            "New York": "New York, United States",
            "London, UK": "London, United Kingdom",
            "London": "London, United Kingdom",
            "Berlin, Germany": "Berlin, Germany",
            "Berlin": "Berlin, Germany",
            "Amsterdam, Netherlands": "Amsterdam, Netherlands",
            "Amsterdam": "Amsterdam, Netherlands",
            # US city deduplication
            "New York City, United States": "New York, United States",
            "New York City": "New York, United States",
            "Silicon Valley": "San Francisco, United States",
            "Silicon Valley, USA": "San Francisco, United States",
            "Silicon Valley, United States": "San Francisco, United States",
            # Vague locations
            "Global": "Global",
            "Europe": "Europe",
            "Unknown": None,
            "N/A": None,
            "": None,
        }

    @property
    def location_suffix_replacements(self):
        """Country suffix fixes applied to location strings."""
        return [
            (", USA", ", United States"),
            (", US", ", United States"),
            (", UK", ", United Kingdom"),
            (", Türkiye", ", Turkey"),
        ]

    @property
    def location_inline_replacements(self):
        """Inline character fixes for location strings (e.g. Istanbul -> İstanbul)."""
        return [
            ("Istanbul", "İstanbul"),
            ("Izmir", "İzmir"),
        ]

    @property
    def domestic_markers(self):
        return [
            "turkey", "türkiye", "istanbul", "İstanbul", "ankara", "izmir", "İzmir",
            "antalya", "bursa", "konya", "adana", "gaziantep", "mersin", "kayseri",
            "eskişehir", "eskisehir", "trabzon", "samsun", "denizli", "malatya",
            "diyarbakır", "erzurum", "kocaeli", "sakarya", "tekirdağ", "gebze",
            "bolu", "düzce", "muğla", "aydın", "manisa", "çanakkale", "edirne",
        ]

    @property
    def major_cities(self):
        return ["İstanbul", "Ankara", "İzmir", "Bursa", "Antalya", "Gaziantep", "Kocaeli", "Eskişehir"]

    @property
    def funding_sanity_cap(self):
        return 500_000_000

    # ── LLM Prompt Fragments ─────────────────────────────────────────────────
    @property
    def pass_1_context_rules(self):
        return """TURKISH CONTEXT RULES:
- Turkish company names often end in A.Ş., Ltd.Şti., or Teknoloji — preserve these suffixes exactly.
- Turkish funding uses period as thousands separator and comma as decimal (e.g., 1.000.000,00 TL).
- Common Turkish AI terms: yapay zeka, makine öğrenmesi, derin öğrenme, bilgisayarlı görü, doğal dil işleme.
- Major startup hubs: İstanbul, Ankara, İzmir, Bursa, Antalya, Gaziantep, Kocaeli, Eskişehir."""

    @property
    def pass_2_sector_context(self):
        return """TURKISH SECTOR CONTEXT:
- Turkish defense startups often serve TSK/SSB/ASELSAN ecosystem. Classify under Defense.
- İstanbul-based SaaS companies serving MENA/EU markets are common — still classify by product domain, not geography.
- "Teknoloji A.Ş." suffix does not imply Technology sector — classify by actual product/service.
- Banks (Garanti, İşbank, etc.) and holdings are Corporate, not Startup.

CLASSIFICATION EXAMPLES (learn from these):
  1. "AI-powered accounting software for SMEs" → Finance | Fintech (NOT Technology | Software Development)
     Why: Primary value is financial, not generic software.
  2. "Cloud infrastructure for ML model deployment" → Technology | AI Infrastructure (NOT Technology | Cloud & SaaS)
     Why: Core purpose is AI model serving.
  3. "Communication platform with chat features" → Technology | Software Development (NOT Health | Healthtech)
     Why: Messaging is not healthcare.
  4. "AI drone platform for agricultural monitoring" → Agriculture | Agritech (NOT Technology | Drones & Robotics)
     Why: The industry served is agriculture.
  5. "Automated insurance claims processing" → Finance | Insurtech (NOT Technology | Software Development)
     Why: The domain is insurance, AI is the delivery method.

CRITICAL RULES:
- If the company description mentions a SPECIFIC industry (healthcare, finance, agriculture, logistics), the Sector MUST reflect that industry, not "Technology".
- "Software Development" is a LAST RESORT category — only use when the product has no domain-specific application.
- "General | Uncategorized" should NEVER be used if there is ANY indication of the company's industry."""

    @property
    def pass_3_formatting_rules(self):
        return """Rules:
1. Turkish names MUST use proper native Turkish characters (ç, ğ, ı, ö, ş, ü, İ). Convert ASCII approximations to proper Turkish.
   Common corrections: Sahin→Şahin, Ozturk→Öztürk, Caglar→Çağlar, Guler→Güler, Isik→Işık.
2. Format funding as Decimal Comma (e.g., "1.250.000,00 USD"). Turkish format uses period as thousands separator.
   If the source says "5 milyon TL", convert to "5.000.000,00 TRY". If unknown, output "Unknown".
3. Investors as a single comma-separated string. If unknown, output "Unknown".
4. Tech_Mentioned: frameworks, languages, tools from sources. "None" if non-tech company.
5. Tech_Assumed: logical inference from product type. "None" if non-tech company.
6. Check consistency: if entity_type is "Media" or "Teknopark", Tech should be "None".
7. City must be a real Turkish city name with proper characters: İstanbul (not Istanbul), İzmir (not Izmir), Eskişehir (not Eskisehir)."""

    @property
    def tag_keyword_overrides(self):
        """Manual keyword overrides for critical tags (higher weight than learned keywords)."""
        return {
            "Finance | Fintech": ["banking", "payment", "credit", "lending", "insurance", "accounting", "finans", "ödeme", "kredi"],
            "Health | Healthtech": ["clinical", "patient", "diagnosis", "medical", "hospital", "pharma", "sağlık", "hasta", "tıp"],
            "Agriculture | Agritech": ["farming", "crop", "soil", "irrigation", "livestock", "agriculture", "tarım", "çiftçi"],
            "Technology | AI Infrastructure": ["model training", "inference", "gpu", "llm", "foundation model", "mlops"],
            "Technology | Cybersecurity": ["threat detection", "vulnerability", "security operations", "siem", "zero trust"],
        }

    @property
    def investor_domestic_instruction(self):
        return "From the portfolio_companies list, identify which ones are TURKISH startups (or have Turkish operations). List them separately."

    @property
    def investor_character_rules(self):
        return "Turkish names MUST use proper Turkish characters (ç, ğ, ı, ö, ş, ü, İ)."

    @property
    def funding_format_description(self):
        return 'Decimal Comma format (e.g., "50.000.000,00 USD")'

    # ── Search Query Builders ─────────────────────────────────────��──────────
    def investor_search_queries(self, investor_name):
        return [
            f'"{investor_name}" venture capital investor Turkey portfolio',
            f'"{investor_name}" yatırımcı Türkiye portföy şirketleri',
        ]

    def startup_search_queries(self, company_name):
        return [
            f'"{company_name}" startup Turkey AI',
            f'"{company_name}" girişim Türkiye yapay zeka',
        ]

    # ── International Company Blocklist ──────────────────────────────────────
    @property
    def international_blocklist(self):
        """Well-known international companies that should not be created as domestic startups."""
        return {
            'openai', 'anthropic', 'google', 'meta', 'microsoft', 'amazon', 'apple',
            'nvidia', 'databricks', 'airbnb', 'uber', 'stripe', 'spacex', 'bytedance',
            'tiktok', 'perplexity', 'mistral', 'hugging face', 'stability ai',
            'runway', 'groq', 'verizon', 'starling bank', 'wayve', 'improbable',
        }

    @property
    def invalid_company_names(self):
        """Placeholder/invalid names the LLM might return."""
        return {
            'unknown', '[company name]', 'company', 'startup', 'n/a', 'none', '',
            'türkiye', 'turkey', 'istanbul', 'ankara', 'ai', 'artificial intelligence',
            'yapay zeka', 'the company', 'this company',
        }

    @property
    def startup_confirmation_query_template(self):
        """Query template to confirm a company exists in this region."""
        return '"{name}" startup OR şirket OR company Turkey OR Türkiye'

    # ── Search Escalation Query Templates ────────────────────────────────────
    @property
    def search_escalation_founders_suffix(self):
        return 'kurucuları OR founders OR CEO'

    @property
    def search_escalation_funding_suffix(self):
        return 'yatırım turu funding raised'

    @property
    def search_escalation_founded_suffix(self):
        return 'ne zaman kuruldu founded'
