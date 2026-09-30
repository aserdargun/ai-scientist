# Yerel Qwen → Director bağlantısı

Tarih: 2026-09-25. Güncel kaynak/imaj/tam kalite sınırı **harness 0.18.0**:
**226 test, 66 strict mypy kaynağı, yedi komut exit 0**. GPT-6 Luna / high
şema kısıtı, tek onarım, kalıcı çağrı kaydı ve hata sonrası durum düzeltmesini
uyguladı. Son gerçek altı öneri denemesi 0.17'de S2 biçim hatasıyla başarısız;
iki S1 önerisi ve 44 Scorer sonucu korunuyor. 0.18 gerçek denemesi baseline sırasında dış GPU tüketicisi nedeniyle durdu;
yerel model çağrısına ulaşmadı. Canlı AOS değişmedi. Aşağıdaki önceki hazırlıklar tarihseldir.

### 0.18 kaynak, imaj ve kalite kaydı

- İmaj: `sha256:9086ab580cdcab462531c867468af6749b1203b6086bbcab1915d6bfc7c2bbdd`;
  session 17801 / exit 0; build ve 74 dosyanın byte parity kontrolü başarılı.
- Tam kapı: **session 59093 / exit 0**; 226 passed, 11 deselected,
  strict mypy 66 kaynak, Pylint 9.33/10, Ruff/Bandit/wheel/import başarılı.
- Sabit son kaynaklarda gerçek PG: çağrı kurtarma **29/29**, session 21544;
  başarısız bölümden devam **30/30**, session 33403; ikisi de exit 0.
  Kanıtlar `local-qwen-attempt-recovery-018-final.json` ve
  `local-qwen-abandonment-018-final.json`.
- Dispatcher **51/51 / exit 0**: `director-failure-state-return-review.json`.
  Hataların yanında, terminal durum yazmadan normal dönen worker da işçisiz
  running bırakmaz. Açık experiment varsa gerçek SQL terminal koruması sürer.
- Bağlayıcı kayıt `local-qwen-018-quality-gate-binding.json`, **11/11**;
  harness SHA `bd89a5385ba0cea4bd12afeeb239787821271b725006cc5a90234d976cef6f0d`.
  Derlenen CPU şeması son üretim `CandidateProposal` şemasıyla aynıdır.

PG kontrollerinde model/tokenizer/worker yürütmesi belirtilen yerlerde
fixture'dır. Olağan bütçe sonu raporu, gerçek PAUSED ve tam drain/kurtarma,
altı gerçek öneri/S2, public suite, eğitim ve AOS GPU birlikte ilerleme açıktır.
Bu sürüm genel M0 kabulünü kapatmaz.

## 0.18 çağrı kaydı ve kesinti kontrolü

`review_local_qwen_attempt_recovery.py` gerçek PostgreSQL checkpoint
yazımından hemen sonra kayıp enjekte edip yeni Director oturumu ve sağlayıcı
açtı: **26/26, gerçek exit 0** (`local-qwen-attempt-recovery-review.json`).

- İlk yanıt tamamlandıktan sonraki kesintide ilk model çağrısı tekrarlanmadı;
  onarım tamamlandı, iki ham nihai yanıt ve 200 giriş/100 çıkış fixture tokenı
  korundu. Aynı kayıt yeniden okunabildi.
- İlk çağrının yalnız başlangıcı kayıtlıysa sonuç belirsiz kabul edildi;
  model tekrar çağrılmadı ve geçerli aday uydurulmadı.
- Onarımın yalnız başlangıcı kayıtlıysa onarım tekrar çağrılmadı;
  ilk çağrının ölçülmüş kullanımı korundu.
- Kaynak hash'leri sabit kaldı; yalnız incelemenin kendi run satırı temizlendi.

İncelemede bulunan kimlik ve kalan süre riskleri ayrıca doğrulandı:
`local-qwen-attempt-recovery-identity-review.json`, **29/29, exit 0**.
Değiştirilmiş sağlayıcı/config ve registry kimlikleri çağrı tekrarı olmadan
reddedildi. Kalan bir saniyede tamamlanmış ilk yanıtın tüketimi geri okundu;
yeni onarımın aktivasyonu reddedildi. Bu kayıt sınırı kontrolü, tüm koşunun
kalıcı son tarihini yenileme yetkisi vermez.

Model runtime'ı, tokenizer sayımı ve son experiment insert sınırı fixture'dır.
Gerçek süreç öldürme, GPU, Scorer, tüm araştırmanın yeniden başlatılması veya
başarısız bölümden sonraki ordinal'e ilerleme bu kontrolün kapsamı değildir.
Sonraki gerçek wire incelemesinin ledger doğrulayıcısı da artık tüm completed
attempt prompt/yanıt/şema eşliğini, tek onarım hakkını ve ilk bozuk yanıtın
korunmasını kontrol eder. Bunun fixture uyum ve değiştirilmiş ilk yanıtı
reddetme kontrolü exit 0 verdi; gerçek altı öneri denemesi henüz tekrarlanmadı.

### Başarısız onarımdan sonra döngünün devamı

`review_local_qwen_abandonment.py` gerçek PostgreSQL, üretim sağlayıcı/bütçe
uzlaştırması ve Director döngüsünde üç yolu çalıştırdı: normal başarısız
onarım; bütçe kaydı yazıldıktan sonra kayıp; abandoned kaydı yazıldıktan
sonra kayıp. Üçü de ikinci ordinal'e ulaştı: **30/30, session 68567 / exit 0**,
`local-qwen-abandonment-after-review.json`.

İlk ve tek onarım toplam iki fixture model çağrısı olarak kaldı; 300 token
bir kez ücretlendirildi, rezervasyonlar kapandı, şampiyon değişmedi. Bozuk
yanıttan geçerli proposal checkpoint'i veya experiment satırı oluşturulmadı.
Kaynaklar kontrol boyunca sabitti. İlk kontrol, loop-state `schema` alias'ının
canonical serileştirmede kaybolduğunu gösterdi; exit 1 sonucu
`local-qwen-abandonment-review.json` içinde korunur. Alias düzeltildikten
sonra başarılı kontrol yapıldı.

Bu kontrolde model/tokenizer, kalibrasyon ve süit kimliği fixture'dır;
ikinci ordinal'e girişte test durur. Docker/Scorer, gerçek model üretimi,
tam koşu raporu veya genel dispatcher hatasının terminal durumu kabulü değildir.

### Dispatcher hata durumu

`review_director_failure_state.py` gerçek Director rolüyle PostgreSQL
claim/event/durum geçişlerini sınadı: önce **25/25**, ardından gerçek açık
experiment kaydıyla SQL terminal koruması dahil **31/31, exit 0**
(`director-failure-state-fence-review.json`). Kaynaklar sabitti.

Worker'ın claim ettiği temiz koşu hatada `failed` oldu ve yalnız hata sınıfı
kaydedildi; rapor oluşturulmadı. Eşzamanlı stop/terminal durumları korundu.
Claim öncesindeki hata veya zaten running olan başka yürütme durumu değiştirilmedi.
Açık fixture baseline kaydı, `failed` geçişini gerçek SQL korumasıyla engelledi;
dispatcher `stop_requested` ve `run.dispatch_recovery_required` kaydetti.
Experiment `proposed` kaldı; terminal belge veya ölçüm uydurulmadı.

Registry/girdi doğrulaması ve `_run_director` yürütmesi bu kontrolde fixture'dır.
Aktif Docker/Scorer/model süreçlerinin drain'i, kurtarma işinin tamamlanması ve
eski 0.17 koşusunun uzlaştırılması bu kanıtın dışında açık kalır.

## Gerçek model denemesinin girdileri

`review-evidence/review_local_qwen_inputs.py` dört deterministik sentetik EVT
görevi hazırladı. Her görev iki sensör, 128 train ve 128 evaluation satırı
içerir. Fixture kimliği `7089275f-d7d2-4f46-b04b-27e809d15326`; bu bir Lab
araştırma koşusunun başlatıldığı anlamına gelmez. Gerçek run kimliğini API
oluşturacaktır.

- Girdi hazırlama: gerçek exit 0, altı kontrol geçti;
  `review-evidence/local-qwen-smoke-input-preparation.json`.
- Scorer girdilerinin kurulumu: gerçek exit 0, yedi kontrol geçti;
  `review-evidence/local-qwen-smoke-profile-preparation.json`.
- Dört profile ve 512 etiket yalnız bu fixture'ın PostgreSQL kayıtlarına
  kuruldu. Etiketler süit dosyasında bulunmuyor. Üretim `load_suite_manifest`
  fonksiyonu Planner rolüyle dört görevi ve hash eşliğini doğruladı; Scorer
  rolüyle etiketlerin tam eşliği kontrol edildi.
- Proposal senaryosu veya sahte model çıktısı hazırlanmadı. Yeni run/task,
  Docker deneyi, model ya da GPU işlemi başlatılmadı. Bu sentetik hazırlık,
  gerçek kamu verisi kabulünü karşılamaz.

Gerçek kurulum komutu:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python \
  docs/ai-scientist/review-evidence/review_local_qwen_profiles.py \
  --preparation docs/ai-scientist/review-evidence/local-qwen-smoke-input-preparation.json \
  --output docs/ai-scientist/review-evidence/local-qwen-smoke-profile-preparation.json
```

Hazırlanan özel süit dosyası:
`data/runtime/local-qwen-review/7089275f-d7d2-4f46-b04b-27e809d15326/suite.json`.
Fixture daha sonra aşağıdaki gerçek 0.17 araştırma koşusuna bağlandı.
Aynı yardımcı `--cleanup` ve yeni bir `--output` yolu ile yalnız bu dört
profile'ı temizleyebilir; `run_tasks` referansı varsa temizlemeyi reddeder.

## Sonraki gerçek doğrulama

Üretim kayıt defterinin seçtiği `local-qwen` sağlayıcısı, API'nin oluşturduğu
queued işi mevcut Director dispatch yoluyla yürütecek. Model kimliği, profil,
şablon, gerçek token tüketimi ve runtime kaydı trusted host tarafından
oluşturulup deney kaydına bağlanmalı. Ham seriler, etiketler ve modelin
uydurduğu skorlar bu sınırı geçmemeli.

En az altı gerçek öneri, en az iki S1 ve iki S2 çağrısı, gerçek Docker guard,
bağımsız Scorer ve Referee ile doğrulanacak. Kısa aritmetik doctor yanıtı
deney yerine sayılmaz. Kesilmiş S2 yanıtı veya S1'e düşüş geçerli S2 deneyi
sayılmaz. Süre ve token bütçesi bekleme, yükleme ve gerekli tekrarları
kapsamalı; gerçek tüketim rezervasyon tavanına kırpılarak gizlenmemeli.

0.17 kaynakları gerçek model denemesi sırasında donduruldu; o sürümün tam
kalite kapısı aşağıda doğrulandı. AOS ile gerçek eşzamanlı ilerleme, agent tool/repair döngüsü,
tam kamu verisi araştırması ve diğer açık M0 kabulleri ayrıca gereklidir.

## Deney gözlemcisi ve çalıştırma sürücüsü

`review_local_qwen_observer.py`, özel bir inceleme veritabanındaki tüm model
çağrılarını izler. Parent servis kimliği, modelin gerçek systemd InvocationID,
PID başlangıcı ve cgroup eşliği bağlanır. Temizlik yalnız bu kimliklerle yapılır.

Gözlemci gerçek kısa CPU servisleri ve açıkça sentetik SQLite bağlarıyla
sınandı: **7/7, gerçek exit 0**. İki ayrı model-servis örneği gözlendi; farklı
parent InvocationID reddedildi; yalnız kayıtlı üç servis durduruldu; gözlemci
dışındaki dördüncü test servisi çalışmaya devam etti. İnceleme sonunda o servis
de kendi test kimliğiyle temizlendi. `local-qwen-observer-final-cpu-review.json`
güncel gözlemci hash'ini tutar; önceki ara sürümün kontrolü ayrıca korunur.
Bu kontrol gerçek GPU drain, AOS veya model çağrısı kabulü değildir.

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python \
  docs/ai-scientist/review-evidence/review_local_qwen_observer_check.py \
  --output docs/ai-scientist/review-evidence/local-qwen-observer-final-cpu-review.json
```

`review_local_qwen_wire.py` ve `review_local_qwen_ledger.py` gerçek koşunun
inceleme sürücüleridir. Wire sürücüsü 0.17 denemesini çalıştırdı; Director'ın
hata ile çıkması nedeniyle altı öneri ledger kabulüne ulaşılamadı.
0.18 sağlayıcı, kayıt defteri ve receipt arayüzleri kararlı olduğunda sürücüler
yeniden bağlanacak. Sürücü API üzerinden altı deney ister; kendi deney döngüsünü
kurmaz. Sağlık/durum gecikmeleri, host rezervleri ve bütün model süreçleri
izlenir. Sonuçta gerçek model çıktısı → aday kaynak → terminal kayıt → bağımsız
Scorer ve Referee karar eşliği doğrulanır. Ledger sonraki replay incelemesi
için korunur; tamamlanmış HTTP durumu tek başına başarı sayılmaz.

İncelemede düzeltilmesi istenen üretim noktaları: S2 düşünme/çıktı bütçesi
uyuşmazlığı, fallback'in ilk çağrı tüketimini kaybetmesi, başarısız model
yanıtının kalıcı kullanım kaydı, aday arayüzünün promptta açık olması ve
gerçek token sayımına uygun bağlam sınırı. Bu maddelerin düzeltme ve kaynak
doğrulamaları aşağıda kaydedildi.

## Kalıcı sağlayıcı kaydının geri okunması

Root incelemesi, sağlayıcı çıktısının JSON'a yazıldıktan sonra strict olarak
geri okunamadığını gerçek CPU kontrolüyle yeniden üretti. `schema` alan adı
ile `schema_version` serileştirmesi uyuşmuyordu; hem bağımsız receipt hem
iç içe ProposalTurn başarısız oldu. İlk sonuç
`local-qwen-receipt-roundtrip-before.json` içinde korunur.

Luna düzeltmesinden sonra root aynı yolu ve checkpoint/trajectory için
canonical JSON yollarını yeniden kontrol etti: **5/5, gerçek exit 0**
(`local-qwen-receipt-roundtrip-after.json`). Dört geri okuma ve kaynakların
kontrol boyunca değişmemesi doğrulandı. Runtime açıkça fixture ile
değiştirildi; GPU, gerçek model, PostgreSQL veya AOS kullanılmadı.

Henüz çalıştırılmayan wire sürücüsü, CLI'ın sabit özel GPU dizininde benzersiz
bir SQLite dosyası kullanacak şekilde güncellendi. Ledger sürücüsü her
çağrının işlenmiş prompt hash'ini ve kayıt defteri kimliğini kontrol eder.
Bu hazırlık, gerçek altı deney kabulünü kapatmaz.

Root daha sonra üretim `register_proposal_before_execution` yolunu gerçek
PostgreSQL oturumları/checkpoint'leri ve fiziksel bloblarla çalıştırdı:
**24/24, gerçek exit 0** (`local-qwen-checkpoint-review.json`). S1 ve S2→S1
senaryolarında kayıt sınırına enjekte edilen hatadan sonra yeni Director
oturumu aynı öneriyi geri okudu; model yeniden çağrılmadı. Receipt, iki
çağrının token toplamı, prompt ve kalıcı kayıt hash'leri korundu. Değişen
sağlayıcı/kayıt defteri kimliği reddedildi. Runtime ve son ledger insert
çağrısı açık fixture olduğundan bu sonuç gerçek model veya SQL kalibrasyon
kabulü değildir. İlk denemedeki fixture imza uyuşmazlığı ayrı `before`
kaydında tutuldu; yalnız incelemenin kendi PostgreSQL satırı temizlendi.

## Gerçek tokenizer ile bağlam sınırı

### Şema kısıtının CPU uyumu — 0.18 hazırlığı

`review_qwen_json_schema.py`, kurulu vLLM `ResponseFormat` dönüşümünü ve
gerçek pinned Qwen tokenizer ile XGrammar derlemesini ayrı CPU sürecinde
sınadı: **7/7, session 55624 / exit 0**. Tam `CandidateProposal` JSON'u
kabul edildi; Markdown sarmalı, bilinmeyen hareket, modelin eklediği verdict
ve eksik kaynak alanı reddedildi. vLLM şemayı değiştirmeden taşıdı;
CUDA başlatılmadı. Süreç 2 GiB/1 CPU, 90 saniye sınırındaydı; kaynak ve
tokenizer hash'leri değişmedi.

Kanıt `qwen-json-schema-cpu-review.json`; şema SHA-256
`5e41218eeb0ef7e20f262a85780b7bd479ebbdc7f89b7118888c2b997ac63ed0`.
Bu ölçüm Pydantic sözleşmesinden üretilen şemayı kullandı. 0.18'in son
sağlayıcı şeması ve gerçek S2 üretimi ayrıca bağlanıp doğrulanmalıdır;
onarım veya araştırma kabulü henüz verilmedi.

### 0.17 prompt sayımı

Pinned tokenizer/config/template dosyalarının SHA-256 eşliği doğrulandı.
Yalnız CPU kullanan, çevrimdışı, 2 GiB/1 CPU sınırındaki ayrı süreçte gerçek
`Qwen2Tokenizer` çalıştırıldı; ağırlıklar veya GPU yüklenmedi.

Mevcut byte hesabının kabul ettiği sentetik promptlar ölçüldü:

| Prompt | S1 token | S2 token | Sınır |
| --- | ---: | ---: | ---: |
| İlk öneri, dört görev ve baseline kaynak | 793 | 790 | 2048 |
| Birikmiş sentetik geliştirme geri bildirimi | 2564 | 2561 | 2048 |

Tokenizer süreci exit 0 verdi; kabul kontrolü **exit 1** ile hatayı gösterdi
(`local-qwen-tokenizer-size-review.json`). Byte sayısına dayalı kırpma bağlam
sınırını sağlayamıyor. Gerçek token sayımı ve eski geri bildirimin buna göre
çıkarılması GPU çağrısından önce yapılmalı; sonraki düzeltme aşağıda doğrulandı.
İlk inceleme sürücüsünün Transformers dönüş tipini yanlış işlemesi
`local-qwen-tokenizer-before.json` içinde korunur; sonraki ölçüm açıkça düz
token-ID listesi istedi. Bu ölçüm gerçek model araştırması kabulü değildir.

Düzeltme sonrası üretim `_select_messages` yolu, aynı sentetik bağlamları
gerçek pinned tokenizer ile GPU çağrısından önce seçti. Ayrı bir tokenizer
süreci seçilen promptların sayımını bağımsız doğruladı: **6/6, exit 0**
(`local-qwen-tokenizer-production-review.json`, session 98181). Başlangıç
promptları 793/790 tokenda kaldı; geri bildirimli S1/S2 promptları
**1920/1917 tokena** indi. Görev kartları ve mevcut aday kodu aynı kaldı,
yalnız en eski geri bildirimler çıkarıldı. Prompt ve ayrılan çıktı birlikte
4096 model sınırına sığıyor. Üretim dosyaları ölçüm boyunca değişmedi;
GPU/model ağırlıkları veya AOS kullanılmadı.

## 0.17 kaynak ve kalite sınırı

- Güncel checkpoint kontrolü: `local-qwen-checkpoint-017-final-review.json`,
  **24/24, exit 0**. Model ve token sayımı fixture; gerçek PG oturumları/bloblar.
  İlk 0.17 inceleme sürücüsündeki gerçek tokenizer ile sahte runtime token
  sayısı uyuşmazlığı ayrı başarısız kayıtta tutuldu; üretim doğrulaması gevşetilmedi.
- Güncel gerçek tokenizer kontrolü: `local-qwen-tokenizer-017-review.json`,
  **6/6, session 51602 / exit 0**; aynı 793/790 ve 1920/1917 sayıları.
- Ölçülen süre aşımını kırpmama, kalan bütçe/restart sınırı ve tekrar ücret
  yazmama: `local-qwen-overrun-budget-review.json`, **7/7, exit 0**. CPU
  kontrolündeki saatler açıkça fixture'dır.
- Son imaj `sha256:5ab109a57ee5c09e7809900d74e318ed9d87cebb14a36a7693c1f706e82250dc`;
  session 90535, build ve byte parity exit 0.
- Tam kalite kapısı **session 34296 / exit 0**: 223 test geçti, 11 live test
  seçilmedi; strict mypy 66 kaynak; Pylint 9.33; Ruff, Bandit, wheel ve import
  başarılı. Yedi komutun tamamı exit 0.
- İlk tam kapı session 79768 / exit 1: yeni API/CLI fingerprint yolları test
  fixture'ında eksikti ve iki tokenizer subprocess satırı Bandit incelemesi
  istiyordu. Fixture ve iki satıra özel açıklama düzeltildi. Genel tarama
  kuralları gevşetilmedi. Modül açıklaması dışındaki çalıştırılabilir Python
  AST'si ve gömülü tokenizer programı aynı kaldı; kanıt
  `local-qwen-017-bandit-review.json`.
- Harness SHA `0ec087490b02223bf8177cedb19fe3ae5fdf74d18e3ba5ec5b3bd3a8ced4b765`,
  59 dosya. `local-qwen-017-quality-gate-binding.json` **12/12** kontrolle
  güncel kaynak/imaj/kalite ve sınırlı CPU/PG kanıtlarını birbirine bağlar.

Tam kapı arşivi `evidence/quality-gate-local-qwen-017-final.json` içindedir.
Gerçek altı model önerisi, tool/repair/Explore, tam bağlam/kapasite, kamu verisi,
eğitim ve AOS ile gerçek eşzamanlı ilerleme bu sınırın dışında açık kalır.

## Altı gerçek öneri denemesi — S2 biçim hatasıyla durdu

0.17 kaynakları tam kapıdan sonra `4b48bc2` commit'iyle kaydedildi. İlk
başlatma API/model çalıştırmadan exit 1 verdi: projeye ait GPU runtime dizini
0755 iken CLI 0700 gerektiriyordu. Yalnız bu dizinin izni düzeltildi;
üretim kodu değişmedi. Kayıt: `local-qwen-017-wire-init-before.json`.

Sonraki çağrı session 40641 içinde API'nin oluşturduğu
`b54c9282-ca69-4deb-9e28-ef50e36bf21c` koşusunu başlattı. Altı öneri,
6000 saniye toplam süre ve 350000 model token bütçesi istendi. İlk gözlemde
koşu `running`, gerçek Docker baseline ve bağımsız Scorer ölçümleri sürüyor;
API health/status yanıtları geliyor. Üretim kaynakları deneme boyunca
donduruldu. Son kanıt `local-qwen-017-wire-review.json` dosyasına yazılacak;
bu başlatma kaydı model/araştırma kabulü değildir.

Ara gözlem (`local-qwen-017-first-proposals-snapshot.json`): üç baseline'ın
36 skoru ve ilk **gerçek S1 önerisinin dört görev skoru** kaydedildi; koşu
hâlâ `running`. İlk önerinin immutable checkpoint/blob hash'i okunarak
typed provider receipt doğrulandı. Gerçek sayımlar 860 giriş / 1049 çıkış
token; tokenizer ön sayımı 860 ile eşleşiyor. Runtime receipt başlangıç
78,309 sn, çıkarım 21,455 sn, drain 0,521 sn; GPU tepe 12786 MiB, model
cgroup bellek tepe 10 GiB bildiriyor. Ayrı canlı gözlemde çağrıdan sonra
GPU yeniden 62 MiB'ye indi. Bu ara kayıt altı önerinin veya AOS birlikte
çalışmasının tamamlandığı anlamına gelmez; bu ara andan sonraki sonuç aşağıdadır.

### Nihai süreç sonucu ve hata incelemesi

Session **40641 / exit 1**; üretim Director süreci de **exit 1**.
`local-qwen-017-wire-review.json` başarısız denemeyi ve 849 kaynak gözlemini
korur. İlk iki gerçek S1 önerisi puanlandı; üçüncü S2 çağrısı tamamlandıktan
sonra `ProviderOutputError` ile araştırma durdu. Altı öneri kabulü geçmedi.

S2 için 1476 giriş/1041 çıkış token ölçüldü; tokenizer 1476 ile eşleşti.
Başlangıç 79,197 sn, çıkarım 21,357 sn, drain 0,540 sn. Modelin nihai yanıtı
JSON nesnesini Markdown kod bloğuna sardığı için katı `CandidateProposal`
ayrıştırıcısı reddetti. Tam nihai içerik hash'li blobda, ölçülen 2517 token
maliyeti immutable hata checkpoint'inde saklandı. Tanı için fence çıkarıldığında
nesnenin şemaya uyduğu doğrulandı; bu içerik üretime yeniden verilmedi veya
geçerli S2 önerisi olarak sayılmadı. Gizli reasoning metni incelemeye alınmadı.

Salt okunur `review_local_qwen_failure.py` **36 kontrol / exit 0**:
`local-qwen-017-failure-diagnosis.json`. Beş experiment/trajectory çiftinin
fiziksel blob kimliği ve iki gerçek S1 kararının 44 ham Scorer kaydından
yeniden üretimi doğrulandı; ikisi de `DISCARD`, verdict/delta/ci_low bit eşliği
geçti. Bu teşhis komutunun başarısı, araştırma koşusunun başarısı değildir.

Çalışma boyunca kaydedilen minimum kullanılabilir RAM 18,049 GiB, minimum
boş disk 76,538 GiB. 848 kontrol sorgusunda status en çok 4,503 ms, health
en çok 1,872 ms sürdü. Dış GPU tüketicisi gözlenmedi. Üç model generation'ı
ve GPU context'leri boşaldı; source/config ve 196 runtime paket sürümü sabit
kaldı. Canlı AOS dosyaları veya süreçleri değiştirilmedi.

İki ürün eksiği somutlaştı: spec'teki tek JSON-şema kısıtlı onarım turu yok;
Director hata ile çıktıktan sonra SQL koşu durumu `running` kalıyor. İşçisiz
bu durum başarı/aktif çalışma olarak sunulamaz. Orijinal ledger değiştirilmeden
inceleme için korunuyor; onarım/bütçe/restart ve hata sonrası durum düzeltmeleri
bir sonraki üretim dilimine taşındı. Tam S2 önerisi, altı deney ve AOS birlikte
çalışma kabulü açık kalır.

## 0.18 gerçek deneme — dış GPU tüketicisi nedeniyle durduruldu

Kaynak/imaj/tam kalite kapısı sonrasında `2c7f651` commit'iyle başlayan
`75642033-6e46-4a73-82a9-9e1ecb6066d3` koşusu **session 31486 / exit 1**
ile durdu. 330,428 saniyede ortak kuyruğa kayıtlı olmayan
`/home/cachyos/.venv/bin/python` süreci, `session-19.scope` içinde **3894 MiB**
GPU belleğiyle gözlendi. Kaynak uygulamanın AOS olduğu doğrulanmadı.

Bu anda baseline aşaması sürüyordu: **23 gerçek Scorer sonucu, sıfır yerel
model çağrısı, sıfır model önerisi**. Gözlemci yalnız kendi Director/API
süreçlerini ve sahipliği doğrulanan sandbox konteynerini temizledi; dış
sürece dokunulmadı. Dış süreç sonraki gözlemde kendiliğinden yoktu; son GPU
62 MiB. Kaynak/config ve runtime paketleri değişmedi.

Kanıtlar `local-qwen-018-wire-review.json` ve salt okunur SQL özetini taşıyan
`local-qwen-018-wire-outcome.json`. SQL koşusu `stop_requested`; bir baseline
`scored`, diğeri `primary_running`, rapor yok. Tam terminal kurtarma açık;
ledger yapay olarak completed/failed yapılmadı. Bu deneme **S2/altı öneri
veya AOS birlikte çalışma kabulü sağlamadı**. Kör tekrar yerine ortak GPU
entegrasyonuna devam edilir; kabul sayıları 3/13/6 kalır.
