# Director döngüsü — kabul ve inceleme kaydı

Başlangıç: `abf79eb` (baseline kalibrasyonu, harness 0.10.0). Bu kayıt uygulanmış bir Director veya geçmiş test kanıtı değildir. Sonraki uygulamanın bağlayıcı sınırlarını ve doğrulama planını spec §3.3, §4.2, §7.M0.8–12 ve review ekinden çıkarır. M0'ın kalan public veri, yerel model, GPU ve AOS kapsamı korunur.

## Mevcut durum

- Üç baseline × dört sentetik EVT görevi × üç tohum gerçek Docker/Scorer yolunda ölçüldü. Fiziksel belge/kalibrasyon blobları, immutable receipt ve terminal rapor kanıtı [13-baseline-calibration-review.md](13-baseline-calibration-review.md) içindedir.
- CLI başlangıçta yalnız `scorer drain`, `scorer finalize`, `plan seal` içerir. Genel araştırma döngüsü yoktur.
- Başlangıç API bütçesi en çok 20 deney, 3600 saniye, koşu başına 8192 model token'dır. Bu sınırlar 25 KEEP olmayan deney + 10 EXPLORE kuralını ve birçok tam S2 bölümünü karşılamaz. Yeni uygulama açık, sınırlı koşu bütçeleri ile ayrı bölüm sınırlarını ayırmalıdır; plato kuralını sessizce kaldırmamalıdır.
- `run_events` üzerinde Director'ın INSERT/SELECT/UPDATE hakkı vardır. Deterministik event UUID'si tek başına içerik değişmezliğini veya tek çalışan Director'ı kanıtlamaz. Yeniden başlatma için kullanılan kayıtlar hash ile bağlı, conflict halinde tam içerik karşılaştırmalı ve sahiplik korumalı olmalıdır.
- Mevcut `ReplayManifest` sonlu, boş olmayan score vektörleri ister. Henüz skor ölçülmeden gerçekleşen guard/timeout/crash kayıtları için uydurma sıfır vektörleri kullanılamaz.

## 20 deneylik gerçek kontrol yolu

Sahte LLM yalnız kaynak, hipotez, tahmini delta ve bölüm mesajlarını üretir. Skor, guard sonucu, Referee kararı veya başarılı terminal durum sağlamaz. Aynı Director, Planner, Docker, Scorer, Referee, kayıt ve rapor yolları ileride gerçek yerel LLM tarafından da kullanılmalıdır.

**Sayı:** 20 proposal ordinal'i; üç başlangıç baseline kaydı bu sayıya dahil değildir. Teyit seed'i veya aynı deneyin altyapı tekrarı yeni proposal sayılmaz. Her proposal tek immutable terminal `experiment.v1` + `trajectory.v1` çiftiyle eşleşir.

Senaryonun zorunlu çekirdeği:

| Durum | Gerekli kaynak ve gözlem |
|---|---|
| KEEP | Causal, ölçülen iyileştirme; seed 0 sonucu tek başına final KEEP olmaz. Parent ve child'ın seed 0–1 görev skorları, ikinci Referee kararı ve yeni şampiyon kaydı birlikte görünür. |
| KEEP_SIMPLER | Gerçek kaynak/bağımlılık veya ölçülen süre sadeleşmesi; ölçülen skorlar ve sabit `best_suite` sınırı Referee'ye girer. LLM'in `simpler=true` iddiası yeterli değildir. |
| DISCARD | Arayüz/guard'ları geçen adayın ölçülen sonuçları Referee tarafından atılır; şampiyon değişmez. |
| REJECT | Sabit/NaN/yanlış uzunluk gibi gerçek bir guard ihlali; ölçülmeyen karar sayıları null olur. |
| Aday çökmesi | Gerçek aday hatası; kayıt nedeni altyapı hatasından ayrıdır. Beş ardışık aday çökmesi ayrı bir senaryoda PAUSED üretir. |
| Uzun fit | Gerçek timeout ve yalnız o adayın konteynerinin boşaltılması; `REJECT(timeout)`. Test profili açıkça daha kısa deadline kullanabilir; üretimin 600 saniyelik seed+guard tavanı korunur. |

20'ye tamamlayan öneriler tekrarlanabilir temiz/olumsuz varyantlar olabilir. Karar etiketleri sonuç tablosuna önceden yazılamaz; sentetik verinin ve adayların gerçek ölçümleri gerekli karar çeşitliliğini üretmelidir. `KEEP_SIMPLER` kümülatif sınırı ayrıca kontrollü Referee fixture'ıyla sınanabilir; bu fixture gerçek 20 deneyin yerine geçmez.

## Döngü ve kayıt koşulları

1. **Öneri önce kaydedilir.** Hipotez ve tahmini delta ölçümden önce immutable proposal'a bağlanır. Ajan bölümünde smoke varsa ilk hipotez smoke'tan önce kaydedilir. Aday kaynağı, gerçek Git ağaçları, bağlam ve mesaj baytları hash ile saklanır.
2. **Bağlam doğru sınırdadır.** `inputs_sha256` canonical ajan bağlamının hash'idir; ham train/eval serilerinin hash'iyle karıştırılmaz. Görev kartları, train özetleri ve dev geribildirimi kullanılabilir. Ham değerler, etiketler, holdout/sealed ayrıntıları ajan bağlamına girmez. Eski sentetik API testinin raw-input fixture'ı üretim LLM bağlamı örneği değildir.
3. **Tek çalışan ve yeniden başlatma.** Aynı koşuya iki Director girdiğinde çift öneri, çift slot veya checkpoint atlama olmaz. Kapanma/çökme sonrası son durable sınırdan devam edilir; tamamlanmış deney yeniden önerilmez. Veritabanı plan kilidi alan API'ler kendi connection/transaction'larını açtığı için dışarıda aynı kilidi başka connection ile tutup onları çağırmak deadlock yaratabilir.
4. **Kaynak değişikliği.** Suite'e bağlı harness/image/calibration kimliği her dispatch'te karşılaştırılır. Değişiklik sonraki adayı `harness_hash_mismatch` ile reddeder; yeni ölçüm sessizce eski kalibrasyona eklenmez.
5. **Bütçe.** Baseline, primary, guard, teyit, model ve altyapı tekrar maliyetleri sayılır. Teyit için yeterli bütçe yoksa KEEP kesinleşmez. Toplam duvar saati ve token tüketimi yeniden başlatmada sıfırlanmaz.
6. **Durum ve durdurma.** BASELINE/LOOP/EXPLORE/PAUSED/HOLDOUT_CHECK/REPORT aşamaları kalıcıdır ve status'ta anlaşılır biçimde görünür. Stop yeni öneri/ölçüm başlatmaz, yalnız kendi işleri boşaltılır, terminal belgeler tamamlanmadan nihai rapor oluşmaz.
7. **Plato ve devre kesici.** 25 KEEP olmayan deneme ve 10 EXPLORE davranışı ayrı sınır testiyle doğrulanır. Ardışık aday çökmesi sayacı altyapı hatalarından etkilenmez; altyapı tekrarları sonsuz değildir.

### Şampiyon değişince noise güncellemesi

Spec, başlangıç BASELINE için üç tohumu açıkça ister; sonraki şampiyonlar için tekrar sayısını aynı açıklıkta sabitlemez. Noise mevcut şampiyonun tekrarlarından gelmelidir. Başlangıç robust-z noise'unu tüm koşu boyunca taşımak bu şartı karşılamaz.

Bu uygulamada açık tercih: yeni KEEP veya KEEP_SIMPLER şampiyonu yayımlanmadan önce 0–2 tohumları ölçülür. Karar, parent/child görev skorlarının 0–1 ortalaması ve **önceki şampiyonun** noise'u ile verilir. İkinci tohumda KEEP_SIMPLER da yeniden sınanır; bu, özgün tek tohumlu sadeleşme yoluna ek doğrulama ve maliyettir. Tohum 2 karar ortalamasına katılmaz; yeni şampiyonun sonraki karşılaştırmalarında kullanılacak population `ddof=0` noise'u hesaplamak içindir. Normalizasyon için ilk kalibrasyondaki base/ref/ağırlıklar değişmez.

Ek tekrar `evaluation_kind=confirmation`, `seed=2`, amaç `champion_noise` olarak kaydedilir. Üç ölçümün ayrı giderleri run bütçesine girer. Bütçe veya başarılı ölçüm eksikse şampiyon terfisi kesinleşmez; noise sıfır varsayılmaz. Replay hangi tohumların karara, hangilerinin sonraki noise'a girdiğini ve iki farklı noise değerini açıkça bağlar. Bu tercih henüz üretim Director kabulü değildir.

## Rapor ve replay

- `lab report <run_id>` gerçek ledger/receipt/bloblardan deney sayısı, baseline ayrımı, verdict dağılımı ve şampiyon merdivenini üretir. HTML içindeki aday/hipotez metni kaçışlanır. Çizgi ve tablo sayıları aynı canonical kayıt kümesinden gelir.
- `lab replay <run_id>` tamamlanmış deneylerin bütün gerekçelerini kapsar. Ölçülen karar kayıtlarında görev sırası, weights, normalizasyon, parent/child seed dizileri, noise, simpler, `best_suite`, guard sonuçları ve sürümler hash ile bağlanır. Skorsuz ret kayıtları trusted guard nedeni ve null ölçümlerle ayrı bir sözleşme kullanır.
- Float eşliği yalnız `==` ile ölçülmez; `float.hex()` veya IEEE bit karşılığıyla delta/ci_low kıyaslanır. Eksik/değişmiş blob ve yanlış calibration digest başarılı replay sayılmaz.
- Rapor/LLM/trajectory erişimi holdout sayıları veya metadatasını açıklamaz. On KEEP sonrası holdout biti, kota ve kanarya testi M0.10'da ayrıca ölçülür.

## Kanıt durumu

Bu belge oluşturulduğunda 20 deneylik koşu, Director resume/ownership, HTML rapor ve tam replay henüz uygulanıp ölçülmemiştir. Kabul satırları yalnız gerçek komut, kaynak kimliği ve sonuç dosyaları eklendiğinde güncellenir. Birinci uygulama sentetik EVT ile başlayabilir; diğer zorunlu M0 kaynakları ve metrikleri açık kalır.

### Ölçülen bileşen kontrolleri

- `tests/test_referee_cumulative.py`: 3/3 geçti, pytest ve Ruff exit 0. Sabit en iyi skor 1.0 ve eps 0.01 altında 0.996 → 0.992 → 0.988 sadeleşme zinciri KEEP_SIMPLER → KEEP_SIMPLER → DISCARD üretir. Tam 0.99 sınırı kabul edilir; bir sonraki düşük float reddedilir. Kaynak hash'leri ve çıktılar `review-evidence/referee-cumulative-floor-review.json` içindedir. Referee kaynak kodu değişmedi; Director'ın bu sabit sınırı doğru taşıdığı henüz kanıtlanmadı.
- `review-evidence/review_director_owner_reconnect.py`: yalnız sahip olunan PostgreSQL koşusunda eski Director connection'ı yerel olarak invalidated edilir; yeni Director sahipliği alırken eski Director'ın checkpoint ve blob yazması engellenmelidir. Bu dar kontrol gerçek çalışan yeniden başlatma, Docker drain veya model çağrısı boyunca sahiplik denetimi kabulü değildir. Sonuç ve ölçülen kaynak hash'i `director-owner-reconnect-review.json` içindedir.
- `review-evidence/director-budget-review.json`: 6/6 test ve Ruff exit 0; geçen süre tek kez düşülür, rezervasyon ikinci kez veya başka koşuda kapatılamaz, sonlu olmayan restore sayaçları reddedilir. Kalıcı checkpoint'ten bütçe devamı ve bütün fazların giderleri henüz tam döngüde ölçülmedi.
- `review-evidence/director-event-migration-apply.json`: aktif Lab koşusu yokken 0013 → 0014 göçü exit 0 ile uygulandı. Uygulanmış migration SHA `8d25ed31b9c60b2bc92fd4c31c55b67b51f62abde77024580a9385f557de4fbc`; bu dosya artık değiştirilmez, yeni şema düzeltmeleri yeni migration alır.
- `review-evidence/director-journal-review.json`: gerçek PostgreSQL'de 17/17 kontrol, exit 0. Sıralı checkpoint, hash ile geri okuma, aynı kaydı tekrar yazma, farklı içerik/sıra atlamasını reddetme, tek Director, ayrı bağlantıdaki plan kilidiyle çakışmama, üç runtime rolünün UPDATE/DELETE reddi ve yeni oturumla aynı kaydı okuma doğrulandı. Yalnız sahip olunan geçici koşu/bloblar temizlendi.
- `review-evidence/director-synthetic-scenario-review.json`: dört küçük korelasyonlu sensör görevi ve sentetik aday kaynakları hazırlandı. Etiketler yalnız trusted fixture yükleyicisindedir; aday ve LLM girdisi değildir. Yerel aritmetik KEEP/KEEP_SIMPLER/DISCARD çeşitliliğini gösterir; üretim kararının yerine geçmez.
- `review-evidence/director-scenario-docker-review.json`: bir sentetik görevde altı aday gerçek Docker/guard yolunda beklenen davranışı verdi. Verbose/simple skor blobları bayt düzeyinde aynı; inverse daha düşük VUS üretir; constant, crash ve uzun fit sırasıyla doğru typed nedenlerle reddedilir. Dört toplu kontrol geçti. Önceki pinli image kullanılmıştır; tam güncel harness paritesi, ayrı Scorer veya 20 deneylik Director kabulü değildir.
- `review-evidence/director-proposal-resume-review.json`: gerçek PG checkpoint ve fiziksel bloblarla 7/7 kontrol, exit 0. Ledger çağrısında hata enjekte edildikten sonra yeni sahip aynı öneriyi aldı; provider yalnız bir kez çağrıldı, üç kayıt denemesindeki immutable alanlar/sıra aynı kaldı, değiştirilmiş bağlam reddedildi. Bu testte ledger kaydı bir spy ile değiştirilmiştir; kalibrasyon SQL kabulü veya tam süreç çökmesi doğrulaması sayılmaz.
- `tests/test_director_primary_recovery.py`: altyapı tekrarının başarıyla dönmesi, ölçümden sonraki checkpoint hatasında işi tekrarlamama, süit görevlerinin tek 600 saniyelik seed bütçesini paylaşması ve terminal belge yolunu atlayan durum geçişinin yapılmaması için dört birim kontrolü. `director-primary-recovery-before.json` süre rezervasyonu hatasını gösterir; `director-primary-recovery-review.json` düzeltmeden sonraki 4/4 sonucu ve ölçülen kaynak hash'lerini tutar. SQL/Docker çağrıları bu kontrolde taklittir; gerçek yeniden başlatma kabulü değildir. Sonraki API değişiklikleri için fixture güncellenir ve yeni canlı dilimde yeniden sınanır.
- `review-evidence/planner-candidate-outcome-before.json` gerçek PostgreSQL'de aynı terminal görev sonucunun tekrarında hatayı gösterir. Wrapper mevcut satırı plan/run kilidi altında okuyacak şekilde düzeltildi; producer rolü SQL trigger'ının yazdığı `swapp_lab_planner` değeriyle karşılaştırılır. `planner-candidate-outcome-review.json` 5/5 geçti: ilk yazım, aynı içerikle tekrar, değişen sonucun reddi, tek satır ve doğrulanmış rol. Guard sonucu bu testte enjekte edilmiştir; aday/Docker davranışı sayılmaz. Yalnız sahip olunan koşu/profil satırları temizlendi.
- `review-evidence/director-event-schema-parity.json`: uygulanan 0014 sonrasında `.venv/bin/alembic check` exit 0; metadata için yeni göç gereksinimi çıkmadı.
- `tests/test_director_simplicity_review.py`: AST düğüm sayısındaki azalma tek başına yeterli sayılmaz; satır azalması %5'e ulaşmalı ve bağımlılık değişimi alt küme koşulunu sağlamalıdır. `director-simplicity-before.json` üç ihlali gösterdi. Düzeltme sonrası dört kontrol ve Ruff exit 0; kaynak hash'leri `director-simplicity-review.json` içindedir. Süreye dayalı alternatif sadeleşme, stdlib/üçüncü parti ayrımı ve tam Director kabulü bu dar kanıtın kapsamında değildir.
- `tests/test_director_confirmation_review.py`: altı negatif terfi kontrolü ve Ruff exit 0. Gerçek Referee/terminal önkoşulları üzerinde sentetik skor vektörleri kullanıldı; seed 1 olmadan KEEP, yeni noise için seed 2 olmadan KEEP, yanlış aday/profil, eksik guard ve yinelenen noise görevi reddedildi. Belgeler reddedilen çağrılarda yazılmadı. `director-confirmation-review.json` kaynak kimliğini tutar; bu test SQL veya Docker çalıştırmaz.

### Director canlı ölçümü için imaj

`review-evidence/review_director_image.py`, yalnız `harness`, `lab`, `vendor` kaynakları ve pinli build bağımlılıklarını içeren ayrı kopyadan ağsız imaj oluşturur. `director-vertical-image-review.json` içinde ilk tek-proposal diliminin build exit 0, kısıtlı konteynerde runtime kaynak bayt eşliği ve baseline import exit 0 vardır. İmaj `sha256:c230ebd5baf524422ce6caa6d1327c83e1cba02d880edfb7cb00a9b7cd487612` olarak pinlendi. Bu hazırlık kanıtı bilimsel deney, tam döngü, GPU veya AOS kabulü değildir.

### İlk canlı koşunun okuma hatası

`director-vertical-before.json` ilk koşudaki 36 başarılı baseline ölçümünü ve testin başarısız çıkışını saklar. Üretim `run_one_proposal` çağrısı üç tohumdaki 12 ölçüm ve terminal KEEP belge çiftiyle döndü; sonraki review sorgusu Director rolüne kapalı `experiment_records` tablosunu doğrudan okumaya çalıştığı için reddedildi. Bu sorgu hatası review betiğine aittir. Betik izinli `lab.experiment_record_receipt(text)` ve hash doğrulamalı blob okumasına geçirildi; erişim hakları genişletilmedi. İlk koşunun tamamlanmış raporu yoktur ve tam başarı kanıtı sayılmaz. SQL temizliği yalnız bu koşunun UUID'sini ve profillerini kapsadı.

Bu tek KEEP smoke'unda başlangıç `best_suite=0.0` kontrol değeri kullanılmıştır. Ölçülen baseline süit skoru kayan nokta yuvarlamasıyla yaklaşık `6.94e-16` çıktı. Bu fark büyük iyileşmeli KEEP sonucunu değiştirmez; smoke, ölçülen sabit-best değerinin Director boyunca taşındığını kanıtlamaz. Tam döngü başlangıç değerini kalibrasyondan türetmeli ve sonraki onaylı şampiyonlarla kalıcı durumda korumalıdır.

### Başarılı tek proposal canlı akışı

Düzeltilen `review-evidence/review_director_vertical.py` exit 0 ile tamamlandı. `director-vertical-review.json` koşu `c234c881-721e-4008-b948-66fea754d4bb` için 10/10 kontrolü kaydeder:

- Dört yerel sentetik EVT profilde üç algoritma × üç tohum: 36 gerçek guarded Docker değerlendirmesi ve ayrı Scorer ölçümü. Kalibrasyon SQL receipt ve fiziksel blob olarak donduruldu, hash ile geri okundu.
- Üretim `run_one_proposal` içinde yalnız kaynak/hipotez veren fake provider, ölçüm öncesi proposal kaydı, bütçe/checkpoint, dört görev × seed 0–2: 12 gerçek aday ölçümü. Seed 0 primary, seed 1 teyit, seed 2 sonraki şampiyonun noise ölçümüdür.
- Referee `KEEP`, süit skoru `3.0`, delta `2.999999999999999`, ci_low `2.9999999999999982`, önceki ve yeni noise `0.0`. Experiment ordinal'i üç baseline sonrasında `4`, proposal ordinal'i `1`dir. Terminal `experiment.v1` ve `trajectory.v1` belgeleri atomik kaydedildi; izinli receipt ve gerçek blob baytları eşleşti.
- Plan kapatıldıktan sonra ayrı Scorer finalizer, 48 ölçümlü hash doğrulanmış raporu yayımladı. Aynı kalibrasyon receipt'i terminal koşuda tekrar okunabildi. Temizlik yalnız bu koşu/profillere uygulandı; fiziksel kanıtlar korundu.

Ölçüm boyunca harness `e90a583c5e02bbcfb32b9816ca78bbf136bc5b38f97734c0e0e10e58bb410a5e` ve imaj `c230ebd5baf524422ce6caa6d1327c83e1cba02d880edfb7cb00a9b7cd487612` değişmedi. Aynı kaynakta zorunlu kalite kapısı exit 0: pytest **168 passed / 11 deselected**, strict mypy 58 kaynak, Ruff/Bandit/wheel import 0, Pylint 9.28/10 (mevcut uyarılar). Gate/imaj/harness bağı `director-quality-gate-binding.json` içindedir.

Bu kanıt genel CLI/Director20, çok şampiyonlu sabit-best aktarımı, tam restart/stop, gerçek Git soy ağacı, rapor HTML/replay, public veri, yerel model, GPU veya AOS kabulünü kapatmaz. Baseline başlangıcı burada trusted review sürücüsüdür; üretim baseline/run yaşam döngüsü ve bütün fazların rezervasyon muhasebesi tam döngüde ayrıca doğrulanacaktır. M0.8 ve M0.9 kısmi kalır.

### Tam döngüye geçişte yeniden başlatma düzeltmesi

`director-seed-resume-before.json`, tamamlanmış primary ölçümü, aday çökmesi ve timeout sonrasında aynı seed'e tekrar girişte üç hatayı kaydeder. Tamamlanmış ölçüm immutable checkpoint çatışmasına, iki aday hatası ise mevcut terminal görev sonucuna takılıyordu. Üretim runner artık tamamlanmış seed sonucunu kaydından kurar; Docker/Scorer dispatch'i, görev planı, faz yazımı ve bütçe harcamasını tekrarlamaz.

`director-seed-resume-review.json` **5/5 birim kontrolü, exit 0** ve kontrol boyunca değişmeyen kaynak hash'lerini tutar. Ek iki vaka gerçek evaluator'ın `degenerate_constant_scores` neden kodunu ve seed 1 tamamlandıktan sonraki daha ileri bütçeyle eski seed 0 sonucunun okunmasını kapsar. Immutable checkpoint adaptörü bellektedir; SQL/Docker yerine test adaptörleri kullanılır. Bu kanıt gerçek süreç çökmesi, tüm Director döngüsü veya 20 deney kabulü değildir.

`review_director20_scenario.py` yirmi kaynak/hipotez önerisini hazırlar. `review_director20.py` yalnız ayrı UUID'li sentetik profil/etiket girdilerini kurmak, **üretim CLI** komutunu çağırmak ve izinli receipt/blob/raporları doğrulamak içindir. Skor, guard sonucu veya Referee kararı provider girdisine yazılmaz; baseline ve proposal döngüsü review betiğinde çalıştırılmaz. `--prepare-only` yalnız dört görevli, etiketsiz CLI manifesti ve yirmi öneri dosyası üretmiştir. Tam CLI komutu henüz çalıştırılmamıştır; M0.9 kısmi kalır.

### CLI görev girdisinin gerçek PostgreSQL kontrolü

`review-evidence/review_director_manifest.py` exit 0 ile **8/8** kontrolü geçti. `director-manifest-review.json` ölçüm boyunca sabit loader SHA `4f478969c19540b5d686c17be7edf693cc9df823d4efe867121d27995b518541` değerini kaydeder. Dört sentetik görevin manifest/hash eşliği ve hardcoding guard'a tüm görev kimliklerinin aktarılması doğrulandı. Yanlış profil hash'i, örnek sayısı, etiket alanı, kaynak kimliği ve ayrı bir holdout profilinin kabul edilmemesi gerçek Planner bağlantısıyla denendi. Planner'ın etiket tablosunda SELECT hakkı bulunmadığı ayrıca sorgulandı.

İlk review sürücüsü mevcut fixture profilinin görünürlüğünü değiştirmeye çalıştı; DB'nin immutable visibility kuralı işlemi reddetti. `director-manifest-before.json` bu hatayı korur. Sürücü ayrı bir holdout fixture oluşturacak şekilde düzeltildi; üretim erişim hakları veya DB koruması değiştirilmedi. Her iki koşuda yalnız kendilerine ait run/profil satırları temizlendi. Bu kontrol Docker/baseline/LLM çalıştırmaz ve M0.9'u kapatmaz.

`tests/test_director_baseline_records_review.py`, üç baseline × üç tohum × iki görev için gerçek typed belge oluşturma, immutable kayıt alanlarının eşliği, fiziksel blob yazımı ve dokuz ortak seed rezervasyonunu sınar. SQL ve Docker yerine test adaptörleri kullanır; küçük pytest geçici dosya sistemi için disk rezervi kontrolü bu birim fixture'da kaldırılmıştır. Bu, gerçek host disk bütçesi veya yeni baseline ölçümü kabulü değildir. `test_director_parent_seed_review.py` ayrı olarak, ilk tohumda DISCARD verilen adayın terminal kaydında parent seed 0 karşılaştırmasının korunmasını gerçek Referee aritmetiğiyle sınar.

### Üretim CLI başlangıcında bulunan iki hata

`director20-cli-before.json`, ilk gerçek CLI çağrısının ölçüm başlamadan exit 1 ile durduğunu kaydeder. Yeni koşuda kalibrasyon bulunmazken `baseline_calibration_receipt(uuid)` NULL yerine PostgreSQL `P0001: run has no frozen baseline calibration` hatası üretir. Baseline bootstrap artık yalnız bu kesin SQLSTATE/mesaj birleşimini ilk kalibrasyonun yokluğu olarak yorumlar; diğer veritabanı hatalarını üst katmana iletir. DB izinleri ve uygulanmış migration değiştirilmedi.

`director20-cli-path-before.json`, sonraki denemede sandbox çalışma dizininin içerik adresli blob deposunun altında oluşturulmasını kaydeder. Depo, iki haneli hash shard'ları dışında `sandbox` girişini doğru biçimde reddetti. Bu koşuda da deney kaydı ve skor sayısı sıfırdır. Düzeltme sandbox çalışma alanını blob deposundan ayırmalıdır; depo doğrulaması gevşetilemez. Her iki deneme başarısız kanıt olarak korunur; yalnız kendi SQL fixture'ları temizlenmiştir.

### Başarılı üretim CLI20 koşusu

Çalışma dizini blob deposunun kardeşi olacak şekilde düzeltildi; depo kuralları korundu. `.venv/bin/python docs/ai-scientist/review-evidence/review_director20.py` **exit 0, 8/8** ile tamamlandı. `director20-cli-review.json`, `1e2bcaf6-42ee-4d3c-8d2a-26c055d8cb40` koşusunun kanıtıdır. Başarılı koşu sırasında kod değişikliği veya insan müdahalesi olmadı.

- Üretim `lab run` dört küçük sentetik EVT görevinde üç baseline'ı ve yirmi proposal'ı yürüttü. Fake provider yalnız aday kaynağı/hipotezi verdi; karar veya skor vermedi.
- 36 baseline ölçümünden dondurulan kalibrasyonun digest'i `751f64c5466ee52a902dee6effd409330bbafe66bd72ca65b14fd513827f0bfa`. Adaylar ve bir guard reddinden önce tamamlanmış görevler dahil toplam 80 gerçek Scorer ölçümü oluştu.
- Sonuçlar: **KEEP 1, KEEP_SIMPLER 1, DISCARD 5, REJECT 13**. İki aday çökmesi ve 90 saniyelik test profiliyle gerçek uzun-fit timeout sonrasında döngü ilerledi. Üretim varsayılanı 600 saniyelik seed tavanını korur.
- İki terfi de dört görev × üç tohumla ölçüldü. Parent zinciri güncel kabul edilmiş şampiyonu izledi; tarihsel best `3.0` oldu. Yedi ölçülmüş karar ham Scorer satırları ve kalibrasyondan yeniden kuruldu; verdict, delta, ci_low ve önceki noise bit eşliği geçti.
- 23 adet `experiment.v1` / `trajectory.v1` çifti, aday/mesaj/girdi belge baytları ve hash'leri doğrulandı. Plan kapatıldıktan sonra ayrı finalizer `completed` raporu verdi; 80 skorla eşleşen rapor hash'i `6b63f31fa3a3d692f1cf220eb1799e721e4f91e0fa2056aa05e7335f48822aee`.
- Harness `8f222823168132d098cb9fb8e64f363e8421960c87a842b87d67a5e0c18ad073`, imaj `sha256:c33fb0063cd59608f633bdea90b580dd4b6e1a6ac4364dc3f729c89a5f308120`; ölçüm boyunca değişmedi. `director20-image-review.json` bu koşunun runtime bayt eşliğini, `director20-quality-gate-binding.json` arşivlenmiş kalite kapısı ile kaynak/imaj/CLI bağını tutar. Gate exit 0: 175 test, strict mypy 61 kaynak, Pylint 9.30/10, Ruff/Bandit/wheel/import 0.

Yalnız bu koşunun SQL fixture'ları temizlendi; fiziksel kanıt blobları korundu. **M0.9'un sentetik uçtan uca şartları geçti.** Bu sonuç gerçek model, public veri, GPU veya AOS birlikte çalışma kabulü değildir. Bağımsız review hesaplaması ürün `lab replay` komutunun yerine geçmez; ölçülmeyen guard retleri bu hesaplamada yeniden yürütülmez. Gerçek Git nesne/ref deposu, HTML rapor, holdout, tüm süreç restart/stop sınırları, EXPLORE/S2 ve toplam suite yükleme RAM kotası açık kalır. Heartbeat yalnız bağlantı sahipliğini denetlediğinden stop durumunun dış işlem öncesi ayrıca denetlenmesi sonraki zorunlu dilimdir.

### Stop sonrasında iş kabulü — negatif kanıt

`.venv/bin/python docs/ai-scientist/review-evidence/review_director_stop_admission.py` exit 1 verdi; `director-stop-admission-before.json` beş kontrolden ikisinin başarısızlığını saklar. Gerçek PostgreSQL üzerinde yalnız review'e ait koşu, authenticated ASGI stop isteğiyle `stop_requested` oldu. Buna rağmen üretim `register_proposal_before_execution` öneri sağlayıcısını çağırdı. Sayıcı sağlayıcı çağrı anında hata verdi; model, baseline, kalibrasyon veya Docker çalışmadı. Bu doğrudan giriş sınırı testidir, tam koşu senaryosu değildir.

Aynı Director lease ile checkpoint ekleme girişimi veritabanınca reddedildi, ancak bu redden önce fiziksel blob yazıldı. Böylece bağlantı sahipliği denetiminin tek başına yeni işi durdurmadığı doğrulandı. Kaynaklar kontrol boyunca sabitti, yalnız bu UUID'nin SQL satırları ve geçici test dosyaları temizlendi. Düzeltme; yeni provider/ölçüm/artefakt işlemlerinden önce aktif koşu denetimi sağlamalı, durdurulmuş işin kimlik kontrollü okuma ve recovery yolunu korumalıdır. Gerçek süreç restart, eşzamanlı stop yarışı ve çalışan işin boşaltılması ayrıca açık kalır.

### Stop kabulü düzeltmesi — 0.13.0

Luna'nın düzeltmesi `require_run_active()` denetimini provider öncesi/sonrası, planlama, her yeni Docker fazı, Scorer dispatch ve retry sınırlarına ekledi. Checkpoint blobu artık koşu satırının `FOR UPDATE` kilidi ve aktif durum kontrolünden sonra yazılıyor. Bağlantı sahipliğini denetleyen heartbeat ile terminal kayıt okuma/recovery yolu ayrı kalıyor.

Root tekrarında `director-stop-admission-review.json` **5/5, exit 0**: stop commit'inden sonra provider çağrısı sıfır, checkpoint reddi ve yeni blob yokluğu doğrulandı. Ayrıca `review_director_phase_stop.py` bir gerçek Docker fit'i bitirdi, sonraki fazın callback'inde gerçek API stop'u commit etti ve score girişinin engellendiğini gösterdi (**3/3, exit 0**, `director-phase-stop-review.json`). Her iki test yalnız kendi PG/çalışma alanı fixture'larını temizledi.

Yeni frozen-source imaj `sha256:9b342827934c5ac89ef2ebf78ab647727d31736c01cd42d2c02e877ac281f4ac`, harness `8e1b3bc6c46e55d42b0d4c62d1a6f31d5e59228bf0267f834bb37d557b886cf7`; build/import/bayt eşliği exit 0. Bu faz testi zaten çalışan uzun konteynerin iptalini, admission check ile dış etki arasındaki eşzamanlı yarışı veya tam AOS stop/restart kabulünü kanıtlamaz. Bunlar açık kalır.
