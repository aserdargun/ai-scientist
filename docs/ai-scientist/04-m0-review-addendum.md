# M0 uygulama eki — review düzeltmeleri ve AOS

Tarih: 2026-09-24. Dayanak: kullanıcının mimari review sonrası Luna 6 high ile goal başlatma talebi ve üst klasördeki AOS ile birlikte deney yapabilme ek talebi. Son açıklama bağlayıcıdır: **AOS ve AI Scientist bu mevcut donanımda aynı anda kullanılabilmeli, yazılım veya donanım kaynakları nedeniyle birbirlerini çalışamaz hale getirmemelidir.**

Uygulama sırası: bu ek → orijinal M0 brief → spec v0.9. Orijinal iki dosya değiştirilmeden korunmuştur. Review önerileri bu ekte belirtilen kapsamda uygulama kararına dönüştürülür; diğer öneriler sonraki kilometre taşıdır. Tam M0, özgün kabul maddeleri ve aşağıdaki AOS maddeleri kanıtlanmadan tamamlanmış sayılmaz.

## Çalışma alanı ve gerçek ortam

- Proje çalışma alanı `/home/cachyos/ai-scientist`, paket/servis adı `swapp-ai-scientist`. Bu klasörde yerel Git deposu oluştur, main'den feature branch aç. Mevcut belgeleri taşıyarak kaybetme; canonical kopyalar `docs/ai-scientist/` altında.
- Kodlama ajanı kullanıcının istediği **gpt-6-luna, reasoning effort high**. Bu geliştirme ajanıdır; ürünün runtime LLM'i yerel Qwen'dir. Bulut LLM yasağı ürünün veri/araştırma döngüsüne uygulanır.
- Host: CachyOS, RTX 4070 Ti SUPER 16376 MiB/sm_89, sürücü 615.71.09, yaklaşık 30.5 GiB RAM, 6 fiziksel/12 mantıksal CPU, yaklaşık 103 GiB boş disk. Python 3.12.13 uv içinde mevcut; sistem Python'unu değiştirme. Docker 29.8.1 çalışıyor; vLLM/NVIDIA Container Toolkit kurulumu doğrulanmadı. Başka uygulamalar da host'u kullanıyor.
- Ubuntu/64 GB ifadeleri ölçülmemiş varsayımdı. Bu fark portable kod geliştirmesini durdurmaz. Küçük geliştirme profili P=1 veya P=2 ve toplam bellek rezerviyle başlar. Spec'in P=4 performans/kaynak kapısı açıkça ayrı kalır; P=2 koşusunu P=4 kabulü diye raporlama.
- GitHub remote yok, mevcut `gh` kimliği geçersiz. Yerel çalışma, feature branch, commit ve PR metni hazırlanır; hedef remote/kimlik sağlanana kadar PR kabul maddesi açık kalır. Kullanıcı adına remote/kurum uydurma.
- AOS host-native inference kullanıyor. Lab için native vLLM, spec'in izin verdiği bir seçenektir; Container Toolkit olmaması CPU iskeletini veya native deneyi engellemez. Büyük indirmelerde disk rezervini denetle; çalışan başka konteynerleri/servisleri silme veya durdurma.

## Önce uygulanacak sözleşme düzeltmeleri

1. **Sandbox:** host supervisor güvenilir tarafta; aday fit ve her score/guard çağrısı görev/faz başına ayrı non-root Docker konteynerinde. Ayrı PID/IPC, ağ yok, cap-drop ALL, no-new-privileges, ro rootfs, pids/cpu/memory/output/time bütçeleri ve özel görev çıktısı. Train/eval yalnız ilgili fazın stdin Arrow IPC'sinde. Fit pickle'ı score sandbox'ına özel, salt okunur model artefaktı olarak aktarılabilir; ham veri dosyası değildir. Adayın serialization kodu hiçbir zaman host/Scorer/Director'da çalışmaz. Diğer görev/deney çıktıları paylaşılmaz. UID 1000→1001 yöntemi yerine bu izolasyon sapması ADR olarak belgelenir; §7.M0.3(e) trusted supervisor PID/belleğine erişememe ile doğrulanır.
2. **Causality:** orijinal Ek C'nin sözleşme testlerini sakla. Üretimde aynı donmuş fit modelinden bağımsız score çağrıları, explicit tolerans ve stateful/adversarial fixture'lar kullan. Causality tek sabit kesim testiyle ispat edilmiş sayılmaz.
3. **Metrikler:** PDM onset hesabı maskeyi dikkate alır; zaman sıkıştırılmaz. Sağlıklı maruziyet sıfırsa görev/scorer geçersizliği açık hata olur; NaN ile KEEP/puan yolu yoktur. AlarmPolicy sonlu eşikler ve pozitif integer dwell doğrular. VUS mask/pooling sözleşmesini bir ADR ve bağımsız golden testlerle sabitle; PDM/NRM orijinal zaman ölçeğinde hesaplanır.
4. **Referee:** ilk sürümde Ek C karar eşikleri ve teyit davranışı korunur, validasyon/serileştirme düzeltilir. `KEEP` yalnız dev şampiyon seçimi demektir; güvenilir popülasyon iyileşmesi veya production terfisi ilan etmez. Simülasyon bulgusu acceptance risk kaydında açık kalır. Referee v2 blok bootstrap/bağımsız doğrulama/ardışık hata bütçesi ayrı sürümlü iş olur; bir satırla alpha küçültüp istatistiksel sorun çözüldü denmez.
5. **Seed ve replay:** stochastic adaylar `ctx.seed` alır; baseline 0–2, teyit 0–1, bootstrap 0. Replay; görev sırası, ağırlıklar, normalizasyon, parent/child, seed'ler, noise, best_suite, simpler, guard sonuçları, bağımlılıklar ve sürümleri içerir. Ölçülmeyen karar alanları JSON'da null; NaN/Infinity yasak.
6. **Golden fixture:** önce upstream TSB-AD 1.5 kaynak/digest ve ayrı numpy<2 ortamı doğrulanır; en az 30 beklenen değer bu ortamdan bir kere üretilir, provenance ile dondurulur. Mevcut olmayan fixture varmış gibi davranma. Üretim vendored metrik çıktısıyla beklenen değer türetme. Upstream lisans/NOTICE korunur. Paket uygulama runtime bağımlılığına eklenmez.
7. **Public data:** kısa benchmark oturumları için `public_benchmark` split politikası ayrı sürümlenir: gerçek sıralama ve train-derived pencereyi koruyan embargo, oturum/olay ayrımı ve yeterli train/eval denetimi. SWAPP'ın bir günlük tabanı değişmez. Kullanıcının 2026-09-24 açık kapsam kararıyla TSB-AD-M seçimi **3 endüstriyel (Genesis, GECCO, simüle CATSv2) + 1 sunucu telemetrisi (SMD)** olarak güncellendi. Bu karar özgün dört endüstriyel kaynak şartının yerine geçer. GHL/SWaT için ek izin yoktur ve bu kaynaklar hariçtir. Seçilen dört kaynağın kaynak/semantik/lisans koşulları ve gerçek materyalizasyonu ayrıca doğrulanır; SMD endüstriyel diye raporlanmaz. Eksik veya uygunluğu belirsiz veri açıkça eksik kalır; sentetik smoke veriyle public-data kabulü sağlanmış sayılmaz. Public veri held-out olsa bile ön eğitim kontaminasyonu garantisi verilmez.
8. **Süreçler:** Postgres rollerinde Scorer etikete erişir, candidate/ajan erişmez. API ve AOS'a ham labels/series gitmez. Queue lease/fencing/idempotency ve eksik sonuç reconcile edilir. 10 dk sınırı bir seed değerlendirmesi+guard içindir; teyit ek maliyet olarak deney/run bütçesinde ayrıca rezerve edilip kaydedilir. Toplam bütçe yetmezse teyit ertelenir ve KEEP kesinleşmez.
9. **Araçlar/LLM:** path traversal/symlink, boyut, token, timeout ve tool-schema sınırları runtime'da zorlanır. `base_url` doğrulaması doctor yanında gerçek HTTP istemcisinde de yapılır; implicit proxy/redirect kapalı, izinli hedef kesinleşmiş. M0 adaptörsüzdür. GPU kapasitesi ölçülmeden varsayımsal sayı acceptance'a yazılmaz.
10. **Kalite:** ham Ek C inceleme kaynağıdır; üretim sürümü strict tipler ve belge/test paritesiyle düzeltilebilir. Harness değişikliği sürüm artırır. Kalite kapısı hataları gizlenmez; geniş linter ignore listeleriyle kapı geçilmez. Review kanıt script'i uygulama runtime'ı değildir.

## AOS entegrasyonu M0 kapsamındadır

Kullanıcı ek talebi spec §4.4'te M3'e bırakılan minimum kontrol yüzeyini M0'a çeker. AOS, AI Scientist'i bir uzman araştırma servisi olarak kullanır. AOS Decider/Bonsai ve Lab Qwen farklı rollerini korur; varsayılan modelleri değiştirme.

Önce `/home/cachyos/aos/AGENTS.md`, README, CODEX_KICKOFF ve ilgili mimari/protokol/runtime belgelerini oku. AOS mevcut scheduler'ı kapalı araç/görev kataloglarına sahip; yalnız Lab HTTP istemcisi veya mock bu entegrasyonu bitirmez. Gerekli dar AOS değişiklikleri kullanıcı kapsamındadır. Geçerli Git geçmişi olmayan AOS'ta değişiklik öncesi ilgili dosyaların hash'ini ve workspace dışına çıkmayan yedeğini al; unrelated değişikliklere dokunma. Canlı managed AOS instance'ını hot-patch/restart etme; ayrı opt-in oturum kullan.

Hedef akış:

```text
AOS ai_scientist görevi
  → güncel state/owner lease + policy + sınırlı deney bütçesi
  → Lab.start_run (kalıcı idempotency key)
  → Lab.get_run / Lab.stop_run / Lab.get_report
  → bağımsız terminal durum + ledger/rapor hash doğrulaması
  → AOS verification + kendi trajectory kaydı
```

- Lab minimum authenticated yerel API: start/status/stop/report; kontrollü typed transport, bounded payload. MCP adaptörü eklenirse aynı servis katmanına bağlanır; ikinci Director yaratılmaz.
- Uzun Lab araştırması AOS'un tek foreground job slotunu veya desktop input lease'ini tutmaz. Kısa dispatch eylemi kalıcı dış iş kaydı oluşturur; status/stop/report bağımsız kısa eylemlerdir. Dış araştırma işinin terminal başarısı ancak rapor doğrulamasıyla gelir. Bu sırada AOS başka yetkili kullanıcı görevlerini çalıştırabilir. Paralel AOS geliştirmesiyle dosya sahipliği ve ortak GPU hook'u `06-aos-coordination.md` üzerinden koordine edilir; hazır olana kadar yamalar/testler izole kopyada hazırlanır.
- AOS tarafında açık `ai_scientist` task kind, izinli finite seçenekler ve typed `lab.*` işlemleri. Serbest shell/curl aracıyla bypass yok. Task/approval/lease/idempotency anahtarları host tarafından üretilir. Lab'da `external_origin=aos`, AOS task/run/action ve kendi lab_run_id'si eşlenir.
- AOS SQLite, Lab Postgres kullanır; birbirlerinin tablolarını yazmaz. Start yanıtı başarı değildir. AOS completion yalnız nihai doğrulanmış Lab raporuyla olur. Deney hiç iyileşmese de bütçe içinde tamamlanmış bir araştırma görevi başarılı olabilir; KEEP garantisi verilmez.
- Lab çağrısı yeniden denendiğinde aynı payload/key aynı run'a döner; farklı payload/key uyuşmazlığı reddedilir. Stop idempotent, sahibine bağlıdır. AOS pause/takeover sonrası yeni start yetkisi taşınmaz; aktif Lab işi davranışı açıkça stop veya güvenli drain olarak sözleşmede belirlenir.
- **GPU paylaşımı:** Lab SERVE/TRAIN ve AOS Decider/Bonsai aynı host kaynak yöneticisinin kilit/fencing protokolüne katılır. Sadece boş VRAM'e bakmak kilit değildir. Bir araştırma koşusu veya sürekli açık vLLM servisi GPU'yu süresiz sahiplenemez. Model çağrıları sınırlı süre/token dilimleridir; bekleyen interaktif AOS isteği varsa bir sonraki uygun sınırda VRAM boşaltılır. AOS önceliğine aging/kota eklenerek Lab'ın süresiz aç kalması engellenir. Yükleme/boşaltma da kilit kapsamındadır; mevcut AOS model runtime'ları aynı protokole katılmadan birlikte GPU kabulü geçmez. Kilide katılmayan aktif GPU consumer varsa raporla/bekle; başka süreç öldürme. Model switch maliyeti ve bekleme gecikmeleri ölçülür; sık polling için GPU modeli çağırma. Eşzamanlı model residency yalnız ölçümle sığarsa kullanılabilir. Aksi halde model çağrıları zaman paylaşır; API, UI, ledger ve CPU deneyleri çalışmaya devam eder. Bu fiziksel GPU maliyetini sıfır gecikmeymiş gibi sunma.
- İki kullanım yolu belgelenir: bağımsız `lab run` ve AOS'tan aynı suite/bütçe ile başlatma. Test ve gerçek model raporları ayrıdır.

Ek kabul kapıları:

ID | Kanıt
--- | ---
M0.AOS.1 | Gerçek AOS typed task/tool/policy yolundan sentetik süitle start→poll→report; AOS ve Lab kimliklerinin ve rapor hash'inin eşleşmesi. Fixture DecisionEngine kullanıldıysa açıkça yazılır.
M0.AOS.2 | Aynı idempotency key ile iki start tek Lab run üretir; değişen payload reddedilir; yanlış owner/stale lease/unauthorized tool reddedilir.
M0.AOS.3 | Stop ve takeover/restart senaryolarında deney yeni yetki olmadan tekrar başlamaz; AOS sonucu bağımsız olarak doğrular.
M0.AOS.4 | İki ayrı süreçte AOS/Lab GPU yarış testi: aynı anda tek owner; timeout, crash, stale owner ve recovery kanıtı. Mock kilit testi gerçek model kapasitesi kabulü sayılmaz.
M0.AOS.5 | Yerel gerçek AOS model kararıyla başlatılmış, yerel gerçek Qwen'in S1/S2 çalıştığı kısa araştırma koşusu; çağrılar/kaynak devri ve sonuç kanıtı. Gerçek modeller çalışmadıysa blocked/unverified kalır.
M0.AOS.6 | AOS hedefli testler ve gerekli `scripts/validate_package.py` kapısı, Lab kalite kapısı; iki başlatma yolunun komutları ve sınırlamaları dokümante.
M0.AOS.7 | Bu host'ta AOS etkileşimli görevi ve Lab araştırma koşusu birlikte açıkken her ikisi de ilerler; tek başına/birlikte gecikme, GPU/RAM/CPU tepe, model switch ve kuyruk bekleme ölçülür. OOM, starvation, çapraz servis durdurma veya API kontrol kaybı yok; deneyler ve AOS sonuçları independently verified. Fixture kanıtı gerçek model yük testi yerine geçmez.

## Birlikte çalışma için yazılım ve host sınırları

- AOS `.venv`/lockfile/model servislerini Lab paketleriyle değiştirme. Lab kendi Python 3.12 `.venv` ve pinli servis ortamlarını kullanır; vLLM ve Unsloth ayrı ortam/image. AOS→Lab iletişimi sürümlü transport üzerinden; iki projenin bağımlılıkları tek ortamda birleştirilmez.
- Portlar, process/service adları, Docker Compose project/network/volume adları, veritabanı kullanıcıları, cache/output/lock dizinleri ayrıdır. Kullanılan listener'ları incele; varsayılan 8000'i kapatıp ele geçirme. Host publish loopback; yanlış servis kimliği reddedilir.
- Toplam fiziksel RAM esas alınır; swap RAM kapasitesi sayılmaz. İlk Lab sandbox profili P=1, toplam en fazla 4 GiB sandbox RAM ve 2 CPU; Scorer/API/DB/model yükleme için ayrıca limit ve host/AOS rezervi planlanır. P yalnız beraber çalışma ölçümleri ve kullanılabilir bellek izin verdiğinde artar. M0 P=4 performans hedefi bu host'ta ancak ölçümle kabul edilir, host'u OOM'a zorlayarak değil.
- CPU deneylerinde BLAS thread sınırı, cgroup memory/cpu/pids, sınırlı disk/log/artefakt kotası ve geri basınç zorunlu. En az 20 GiB disk rezervi korunur; büyük model/veri indirmesi ve açılması önceden boyutlandırılır. Ortak model cache kullanılırsa yalnız immutable artefaktlar; başka uygulamanın cache'i temizlenmez.
- Kontrol API'leri model yüklenmesini beklemez; GPU meşgulken health/status/stop çağrıları yanıt verir. Lab stop yalnız kendi çocuk süreçlerini/konteynerlerini etkiler. AOS kapanırsa Lab'ın kimlikli işleri kayıttan uzlaştırılır; Lab kapanırsa AOS UI/başka görevleri devam eder.
- M0 eğitim deneyi kısa, kontrollü bakım profilidir. Uzun TRAIN işi etkileşimli iki-servis profiline sessizce girmez. M0.AOS.7 araştırma inference+CPU deneylerinin beraber kullanımını ölçer; ileride uzun eğitim için ayrı kaynak sözleşmesi gerekir.

## İlk uygulama dilimi

Luna keşif ve `m0-plan.md` ile başlar; ardından çalışır repo/quality gate, düzeltilmiş typed contracts, negatif fixture'lar, Referee/replay temeli ve AOS transport sözleşmesini kurar. Plan onayı için tekrar durmaz. Donanım/veri/remote bağımlılığı olan bir kabul maddesi, bağımsız CPU uygulama işlerini durdurmaz. Her gerçek engel komut ve kanıtla `m0-acceptance.md` içinde açık kalır. Hedefin tamamlanması için tüm zorunlu maddelerin gerçek kanıtı gerekir; başlatma veya mock başarısı M0 tamamlandı değildir.
