# Mimari inceleme — 2026-09-24

İncelenenler: `01-ai-scientist-spec.md` v0.9 (tamamı), `02-luna-goal-brief-m0.md`, gerçek host ve kullanıcının ek talebiyle `/home/cachyos/aos` sözleşmeleri/kritik yürütme kodu. Sonuç: ayrı araştırma servisi, deterministik Referee, dış Scorer, yerel LLM ve ledger ayrımı uygulanabilir. Aşağıdaki blocker'lar çözülmeden belgedeki haliyle güvenilir M0 kabulü verilemez. Uygulama, `04-m0-review-addendum.md` düzeltmeleriyle başlatılabilir.

Bu klasörde başlangıçta yalnız iki Markdown vardı; kod, golden fixture, Git geçmişi veya remote yoktu. AOS kodu var; onun `.git/` dizini de geçerli bir Git deposu değil. AOS genel amaçlı eklenti sunucusu gibi varsayılmamalı.

## Bulgular

§ref | Sorun | Önerilen değişiklik | Önem
--- | --- | --- | ---
§3.2.7, §3.3.5, §7.M0.3 | UID 1000 sürücüsü, tüm capability'ler düşürülmüş ve `no_new_privs` altında UID 1001 başlatamaz; ortak `/out` ve aynı UID ile paralel görevler birbirlerinin çıktılarını değiştirebilir. | Güvenilir host supervisor; görev/faz başına ayrı ağsız, non-root konteyner ve özel çıktı dizini; fit/score'a ayrı stdin; `/proc`, FD, IPC ve çıktı izolasyon testleri. | blocker
§3.2.7, Ek C, §3.9 | Aday pickle'ı kod çalıştırabilen bir artefakttır. Güvenilir sürücü/Scorer'da açılırsa etiket sınırı ortadan kalkar. | Pickle yalnız aday konteynerinde açılır. Dış süreçler yalnız boyut/tip doğrulamalı sayısal çıktı ve strict JSON okur; `np.load(..., allow_pickle=False)`. Symlink, hardlink, özel dosya ve aşırı çıktı testleri. | blocker
Ek C `causality_violation`, §7.M0.4 | İki `score()` aynı nesnede çağrılır; vaat edilen kopyalama yoktur. Ayrıca referans mutlak fark döndürürken guard tablosu bağıl fark ister. | Aynı donmuş fit artefaktından iki bağımsız sandbox çağrısı, çoklu kesim/prefix testi, açık atol/rtol sözleşmesi. | blocker
Ek C `pdm_task_score`, `pdm_earliness` | Maskeli anda başlayan alarm erkenlik puanı alır. Hiç sağlıklı süre yoksa FA/duty NaN olur; karşılaştırmalar false kaldığı için hep açık alarm 1.0 puan alabilir. | Zamanı koruyan maskeler, geçerli onset hesabı, pozitif sağlıklı maruziyet doğrulaması; geçersiz görev/metric sonucu ayrı ve kapalı hata. | blocker
§3.2.3, M0 brief veri listesi | Her veri setine en az bir gün embargo, kısa SKAB deneylerinde train/eval bırakmaz. Örnek `valve1/0.csv` 1147 satır ve yaklaşık 20 dakika. | SWAPP için bir günlük tabanı koru; kamu benchmark'ına özel, train'den türetilmiş embargo ve oturum ayrımı ADR'si yaz; sentetik tarih uzatma yapma. | blocker
§3.2.8, §7.M0.1, brief 3b | 30 golden fixture ve numpy<2 ortamı çıktıları "repoda" deniyor; paket içinde mevcut değiller. | Bağımsız pinli upstream/numpy<2 ortamında bir defalık oluşturma, kaynak hash ve bağımlılık kilidi; aday implementasyondan beklenen değer üretme. | blocker
§3.3.4, §12.1 | Görev bootstrap'ı bağımsızlık varsayıyor; aynı ailede korelasyon ve tekrar kullanılan dev kümesi hesaba katılmıyor. İkinci seed bağımsız yeni görev değildir. Simülasyon bazı null senaryolarda %2 sınırını aşıyor. | Referee v1 kararını yalnız dev seçimi say; genelleme iddiası/terfi yapma. Aile/varlık blokları, bağımsız doğrulama ve kampanya düzeyinde önceden belirlenmiş hata bütçesi için sürümlü v2 tasarla. | blocker (istatistiksel güven iddiası)
§1, §4.4, §6.3; kullanıcı AOS ek talebi | Yerel AOS'ta `Action.tool` ve `State.task_kind` kapalı Literal listeleridir; `lab.*` yok. SQLite trajectory şeması SWAPP F1 ile aynı kabul edilemez. | M0'a gerçek AOS adaptörü ve kabul testi ekle; AOS task/run/action kimliklerini Lab run kimliğiyle eşle; her sistem kendi DB'sine yazar. | blocker (AOS kabulü)
§3.10.5; AOS RUNTIME | Lab'ın kendi Postgres GPU kira satırı AOS Decider/Bonsai yüklenmesini engellemez. vLLM VRAM'in %92'sini ayırırken AOS modeli yüklenebilir. | İki sistemin katıldığı tek host kaynak kilidi, bounded drain/unload ve kimlikli yeniden başlatma; katılmayan GPU kullanıcılarında fail closed. | blocker (birlikte GPU kullanımı)
§3.10.1, brief keşif | Host 64 GB/12 fiziksel çekirdek/Ubuntu varsayımını karşılamıyor: yaklaşık 32 GB, 6 fiziksel/12 mantıksal CPU, CachyOS. NVIDIA Container Toolkit ve vLLM henüz doğrulanmış/kurulu değil. | CPU geliştirmesini izole Python 3.12 ile sürdür; geliştime profilini bellekten türet; gerçek P=4, 32k×2 ve 24k eğitim kabulünü ölçüm yapılana kadar açık bırak. | important
§3.10.4–6 | Verilen vLLM komutu bir ölçüm değil. Model kartı parser'ları destekliyor, fakat Ada+FP8+LoRA+32k×2 bileşimi kanıtlanmış değil. M0 adaptörsüzken LoRA bayrakları gereksiz karmaşıklık ekliyor. | Revision/image pinle; önce adaptörsüz tek dizi, sonra iki dizi ölç. Desteklenirse `--language-model-only`; FP8 → AWQ → llama.cpp sapmalarını kayıt altına al. | important
§2.4, Ek C `Decision`, §7.M0.12 | REJECT için NaN strict JSON'a çevrilemez. Kararı yeniden üretmek yalnız skor dizilerine dayanamaz: ağırlıklar, guard'lar, parent/child seed'leri, noise, best_suite, simpler ve sürüm gerekir. | Wire'da ölçülmeyen alanlar null + neden; replay manifestinde tüm girdiler, hash'ler ve float temsili; eksik artefaktta replay başarısız. | important
§3.2.8 | Maskeyi silmek ayrı olayları birleştirebilir; max-pool ile 50k'ya küçültme pencere boyunu ve FA/gün zaman ölçeğini değiştirir. | Zaman eksenini ve geçerli blok sınırlarını koru; PDM/NRM orijinal ızgarada; VUS downsample sözleşmesi ve sliding_window dönüşümü fixture ile sabitlenir. | important
§3.2.6, §3.9 | Ağ kapalı olması engellenen denemenin audit edildiğini kanıtlamaz. Hata regex'i tek sayı, kodlanmış metin, dosya adı, süre ve skorla dışarı taşıma kanallarını kapsamaz. | Kernel/supervisor denetimi, sınırlı çıktı şeması ve boyutu; kontrollü hata kodları. Regex'i DLP garantisi gibi sunma. | important
§3.2.5, §3.5.5 | REB 3 görev içerebilir, fakat aile tavanı en az 4 aile ister. Dört ailede tavan her aileyi tam %25'e zorlayıp tip paylarını değiştirebilir. | REB en az 4 bağımsız aileli görev; son tip/aile ağırlıklarını raporla, nominal tip paylarının tavan sonrası değişebileceğini belirt. | important
§3.3.1, §6.2 | `SKIP LOCKED` tek başına iş sahipliği ve çökme sonrası dış etki tekrarını çözmez. 10 dk hesabında ikinci seed, baseline ve holdout kapsamı belirsiz. | Lease/heartbeat/fencing, idempotency ve reconcile; bütçeleri run/experiment/seed/evaluation düzeyinde açık tanımla. | important
§3.7, Ek A | `predicted_delta` kaydı sonuçtan önce olsa da önceki 3 smoke sonucundan sonra yazılabilir. KTO REJECT etiketleri altyapı hatalarıyla karışırsa öğrenme bozulur. | İlk hipotezi yamadan/smoke'tan önce kaydet; altyapı/aday/policy nedenleri ayrı; belirsiz ve rollback olmuş sonuçları eğitimden dışla. | important
§3.2.6 `position_bias` | Sağlıklı yük/sıcaklık drift'i meşru skorla monoton olabilir; NRM'lerin yarısı kuralı bunun nedenini ayıramaz. | M0 guard'ını görünür sınırlama olarak tut; driftli negatif kontroller ekle, ileride rejime koşullu kontrolle yeniden kalibre et. | important
M0 brief lisanslar | TSB-AD kod lisansı tüm alt veri setlerinin lisansı değil; dört endüstriyel set adlandırılmamış. Kaynak tablo GHL için lisans yok, Genesis için NC-SA, SWaT için başvuru şartı listeliyor. | İndirme öncesi kaynak/sürüm/kullanım uygunluğu manifesti; belirsiz veri ve ondan türeyen eğitim kayıtları eligible=false. Dört set şartını gizlice sentetik veriyle tamamlamama. | important
§3.2.6, §7.M0.5, brief teknik kurallar | Brief her yerde random_state=0 diyor; teyit seed 1 ve noise seed 0–2 gerektiriyor. | `ctx.seed` kullan; yalnız bootstrap seed ve varsayılan deney seed'i 0; BLAS thread sayısı 2. | important
Ek C, §6.5, brief 3b | Birebir Ek C, eksik type parametreleri/annotasyonlar nedeniyle `mypy --strict` hedefiyle çatışıyor. | Ham kaynak inceleme kanıtı olarak saklanır; üretim kopyası davranış parity testleriyle türlenir ve sürümlenir. | minor
§7.M0.15, brief Git | Remote yok, `gh auth status` geçersiz token döndürüyor; PR bu ortamda şu an açılamaz. | Yerel repo/feature branch ve PR gövdesi hazırlanır; remote/auth sağlanmadan PR açık veya M0 tamamlandı denmez. | important

## Referee simülasyonu ve sınırları

Komut: `OPENBLAS_NUM_THREADS=2 uv run --python 3.12 --with numpy==2.2.6 --with pandas==2.2.3 docs/ai-scientist/review-evidence/reproduce_review.py`.

Python 3.12.13, numpy 2.2.6, pandas 2.2.3. Ek C'nin orijinal testleri geçti. Her senaryoda 10.000 sıfır popülasyon ortalamalı aday; aynı 4000 bootstrap çekimi/tohum 0, eşit görev ağırlıkları ve dört aile. İkinci aşama, ilk aşama KEEP olanlarda iki seed ortalamasına uygulanır. Vektörleştirilmiş hesap gerçek `decide()` ile örnekler üzerinde karşılaştırıldı.

Görev | Görev farkı σ | Seed korelasyonu | Aile içi korelasyon | noise_sd | Teyit sonrası KEEP (%; %95 Wilson aralığı)
--- | --- | --- | --- | --- | ---
12 | .03 | 0 | 0 | 0 | 2.73 [2.43, 3.07]
16 | .03 | 0 | 0 | 0 | 1.66 [1.43, 1.93]
12 | .08 | 0 | 0 | 0 | 5.66 [5.22, 6.13]
16 | .08 | 0 | 0 | 0 | 5.64 [5.20, 6.11]
12 | .08 | 1 | 0 | 0 | 11.80 [11.18, 12.45]
16 | .08 | 1 | 0 | 0 | 11.54 [10.93, 12.18]
12 | .08 | .8 | .6 | 0 | 18.95 [18.19, 19.73]
16 | .08 | .8 | .6 | 0 | 21.60 [20.80, 22.42]
12 | .08 | 0 | 0 | .02 | 0.58 [0.45, 0.75]
16 | .08 | 0 | 0 | .02 | 0.10 [0.05, 0.18]

Bu oranlar yeni görev popülasyonunda sıfır ortalama etki altında genelleme hatasını örnekler. Sabit dev kümesindeki deterministik gerçek iyileşmenin yanlış ölçüldüğü anlamına gelmez. Gerçek üretim oranı değildir; noise_sd tahmin belirsizliği, eşitsiz ağırlıklar ve uyarlamalı aday seçimi modellenmedi. Bu nedenle tek bir evrensel yanlış KEEP olasılığı verilemez. Bağımsız yeni veride geçerli test olmadan alfa harcaması tek başına uyarlamalı dev kullanımını düzeltmez. 4000 bootstrap ile çok küçük kampanya alfa dilimleri de güvenilir tahmin edilemez.

Ek negatif örneklerde aynı nesnede durum değiştiren causal skor için ihlal 1.0; yalnız maskeli konumlarda açık alarm için PDM 0.795918; sağlıklı maruziyet sıfırken hep açık alarm için PDM 1.0 ve NaN FA/duty çıktı. Kanıt: [review-results.json](review-evidence/review-results.json), [orijinal Ek C çıktısı](review-evidence/appendix-c-original-test-output.txt).

## §12 sorularına kalan yanıtlar

1. Referee: yukarıdaki simülasyon; v1 yalnız dev seçimi, genelleme için bağımsız aile blokları ve kampanya düzeyinde doğrulama gerekir.
2. Normalizasyon: kırpılmış etki büyüklüğü operasyonel anlamı korur; rank aykırılara daha dayanıklıdır fakat büyüklüğü kaybeder ve aday havuzuna bağımlıdır. M0'da mevcut normalizasyonu tut, aile ve görev bazlı hassasiyet raporla.
3. PDM: pencere içi ilk onset ve doluluk iyi başlangıç; maskeler ve maruziyet hataları düzeltilmeli. CARE uzun erken uyarı penceresini her örneği doğrulanmış arıza gibi EVT'ye çevirmeme kararı doğru; CARE rapor metriği kalmalı.
4. C-UTIL: robust-z'ye faydayı ölçer; tüm dedektörlere genelleme iddiası olamaz. M2'de önceden sabitlenen ikinci dedektörle duyarlılık kontrolü eklenebilir.
5. Causality: mutlak timestamp, task sırası, pickle, ortak çıktı, modül/global durum ve public benchmark ezberi açık kanallardır. Kopyalama tek başına kanıt değildir; oturum/faz izolasyonu ve metamorfi testleri gerekir.
6. Holdout: tek kontrol biti de bilgi taşır; 20/100 sorgu sınırı matematiksel genelleme garantisi değildir. Kota, geri almalar dahil atomik tutulmalı; public veri mühürlense bile ön eğitim kontaminasyonu olasıdır.
7. Enjeksiyon: yalnız seed değiştirmek üreticinin imzasını değiştirmez. Ayrı jeneratör ailesi, parametre aralığı ve gerçek olay doğrulaması gerekir.
8. REB gücü: 6–10 bağımsız problem sınırlıdır. Normal yaklaşım ve iki yönlü %5/80% güç altında MDE yaklaşık 2.80/√n, yani 1.14–0.89 eşleştirilmiş standart sapmadır; küçük örneklem t düzeltmesi daha büyüktür. Tekrar seed'leri bağımsız problem sayılmaz; pilot varyansı ve güç simülasyonu yapılmadan küçük gelişme vaat edilmemeli.
9. KTO: yalnız belirgin, teyitli, zinciri holdout'tan dönmemiş ve neden kodu temiz sonuçlar; REJECT altyapı hatası değildir. İki kat ağırlık sabit doğru gibi alınmamalı, ablation gerekir.
10. Eğitim çöküşü: çeşitlilik tabanı ve %20 temel model faydalı deney ayarlarıdır, garanti değildir; aile entropisi, held-out transfer ve geri alma kapısı gerekir.
11. S1/S2: M0 deterministik hareket yönlendirmesi denetlenebilir. Belirsizlik skoru ancak kalibre edilmiş sonraki deney olarak eklenmeli.
12. VRAM: model ve parser desteği kaynaklarda var; 24k QLoRA ve 32k×2 kapasite bu host'ta ölçülmedi. 32 GB RAM CPU offload için ayrıca kısıt.
13. Model sayısı: Lab'da tek Qwen iki rol başlangıç için uygun. AOS'un Decider/Bonsai modelleri ayrı ürün rolleri; bunları sessizce Qwen ile değiştirmemeli. Aynı GPU'da zaman paylaşımı gerekli.
14. Mimari: minimal döngü ve Postgres tek host için uygun; fencing, outbox/idempotency, güvenilir Scorer UID/DB rolü ve supervision eklenmeli.
15. Position bias: yukarıdaki drift yanlış pozitif riski; gözlenmiş rampa testini tüm zaman hilelerinin ispatı saymama.
16. Tehditler: pickle, çıktı symlink/FD, eş görev müdahalesi, DNS rebind/redirect/proxy, aynı UID'de servis sırları, GPU kiraları ve public benchmark kontaminasyonu eklenmeli. LLM URL kontrolü runtime HTTP istemcisinde de uygulanmalı.

## AOS entegrasyonu için somut farklar

- `../aos/src/aos/contracts.py`: kapalı `task_kind` ve `Action.tool`; arbitrary Lab tool çağrısı bugün geçmez.
- `../aos/src/aos/computer.py`: `ToolRegistry`, `SafetyPolicy`, `ComputerGateway`; burada yetki/güncel state/idempotency kapıları korunmalı.
- `../aos/src/aos/desktop_tasks.py`: tek aktif `DesktopScheduler`, süreli onay, lease ve engine cleanup. Genel scheduler eklentisi varmış gibi kod yazılmamalı.
- `../aos/src/aos/reusable_decider.py` ve `docs/RUNTIME.md`: model süreçleri native ve yönetilen ömürlü; Lab vLLM'iyle ortak VRAM koordinasyonu bugün yok.
- AOS'a ince bir `ai_scientist` görev türü ve Lab istemcisi eklemek kullanıcı talebinin kapsamındadır. Bağımsız CLI yolu da korunur. AOS'un canlı oturumunu durdurmadan, ayrı opt-in test oturumunda doğrulanır. Kaynak dizinleri gerçek Git deposu olmadığı için değişiklikler yedek/diff/hash ile izlenmeli.

## Dış doğrulama kaynakları

- [Qwen3.5-9B model kartı](https://huggingface.co/Qwen/Qwen3.5-9B): model, düşünme anahtarı ve sunum örnekleri. Donanıma özel kapasite ispatı değil.
- [vLLM Qwen tarifi](https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen3.5.html): `qwen3`/`qwen3_coder`, `--language-model-only`; yayınlanan büyük GPU kurulumları Ada 16 GB kabulü değil.
- [Python pickle sözleşmesi](https://docs.python.org/3.12/library/pickle.html): güvenilmeyen pickle açmanın kod çalıştırabilmesi.
- [Docker container yürütme](https://docs.docker.com/engine/containers/run/): kullanıcı/capability ve kaynak sınırları.
- [SKAB örnek verisi](https://raw.githubusercontent.com/waico/SKAB/master/data/valve1/0.csv): gerçek zaman aralığıyla kısa oturum gözlemi.
- [TSB-AD kaynak deposu](https://github.com/TheDatumOrg/TSB-AD), [alt veri seti tablosu](https://thedatumorg.github.io/TSB-AD/): kod ve veri lisanslarının ayrı olması, kaynak/set listesi.
- [CARE makalesi](https://www.mdpi.com/2306-5729/9/12/138), [sürümlü veri kaydı](https://zenodo.org/records/15846963): erken arıza tespiti bağlamı ve sürüm sabitleme gereği. Tam veri indirimi bu incelemede yapılmadı.

Kaynaklar 2026-09-24 tarihinde kontrol edildi. Bu çalışma canlı LLM, eğitim, veri lisansı kabulü veya üretim güvenlik sertifikası değildir.
