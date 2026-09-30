# AOS dış araştırma işi — entegrasyon review

Tarih: 2026-09-24. Bu dilim `04-m0-review-addendum.md` içindeki M0.AOS.1–3 ve M0.AOS.6 için uygulama yolunu inceler. Canlı AOS üzerinde paralel geliştirme sürer; değişiklikler `data/runtime/aos-coexistence/source-current` kopyasında hazırlanır. Canlı dosyalara veya servislere uygulanmış yama yoktur.

## Önceki kanıt ve bu dilimin sınırı

Lab commit `4097f6a`, gerçek üretim CLI ile üç baseline ve yirmi sentetik proposal'ı tamamladı. Ancak review sürücüsü koşuyu önceden `running` durumunda kurdu. Bu, AOS/API üzerinden başlatmanın kanıtı değildir.

Mevcut Lab API `POST /v1/runs`, bütçe ve suite bilgileriyle `queued` kayıt oluşturur. Mevcut CLI ise `running` kayıt ile immutable request içinde manifest hash'i, scenario hash'i ve proposal limit'i bekler. Kuyruk kaydını üretim Director'ına bağlayan yürütücü henüz yoktur. AOS tarafında HTTP istemcisi hazırlamak bu boşluğu kapatmaz.

## Tamamlanması gereken akış

1. AOS'ta sonlu seçenekli `ai_scientist` görev türü ve typed `lab.start/status/stop/report` eylemleri bulunur. Mevcut state freshness, owner/lease, policy ve yetkilendirme yolu korunur. Model yanıtı suite, bütçe veya araç yetkisini genişletemez.
2. Start eylemi önce gerçek AOS task/run/step/action kayıtlarına bağlanır. Dış iş kaydındaki UUID'ler bu satırlara referans verir; ilişkisiz kimlik üretmek gerçek trajectory değildir.
3. Host tarafından üretilen Lab idempotency key, AOS oturumu ve eylemine bağlanır. Aynı kullanıcı anahtarının iki oturumda kullanılması diğer oturumun Lab koşusunu sahiplenemez. Aynı eylemin ağ hatasından sonra tekrarı aynı Lab koşusuna dönmelidir.
4. Lab kabulü, izinli suite/provider registry üzerinden gerçek immutable manifest ve bütçeye bağlanır. Kuyruktaki kayıt sınırlı ve ayrı bir Director sürecine aktarılır. Kullanıcı isteği serbest dosya yolu, shell veya URL sağlayamaz. AOS kökeni ile task/run/action ilişkisi Lab kaydında da bulunur.
5. Uzun araştırma işi AOS foreground scheduler slotunu veya desktop input lease'ini tutmaz. Start ACK yalnız kalıcı handle'dır. Lab çalışırken AOS başka yetkili bir kullanıcı görevini gerçekten ilerletebilir.
6. Status ve stop kısa, kimlik denetimli işlemlerdir. Önceki oturum/nesil yetkisi yeni göreve taşınmaz. Belirsiz start yanıtı ve process restart sonrasında dış iş handle'ı kaybolmaz. Stop, yeni proposal/model/ölçüm dispatch'ini engeller ve yalnız sahip olunan işi boşaltır.
7. Terminal başarı ancak bağımsız Lab raporu hash'inin, koşu kimliğinin ve terminal durumunun eşleşmesiyle AOS verification'a yazılır. HTTP 200 veya modelin başarı iddiası yeterli değildir.

## Prototip incelemesinde bulunan somut sorunlar

- **JSON UUID ayrıştırma:** `json.loads` sonrası strict `model_validate(dict)`, ağdan gelen UUID string'ini reddeder. Test istemcisinin doğrudan UUID nesnesi vermesi bu kusuru gizliyordu. Gerçek JSON baytları strict JSON ayrıştırıcısıyla doğrulanmalı; loopback HTTP taşıması ayrıca çalıştırılmalıdır.
- **SQLite thread erişimi:** AOS bağlantısı varsayılan thread denetimiyle açılırken bütün coordinator çağrısını `asyncio.to_thread` içine almak yanlıştı. Yalnız ağ I/O'su worker thread'e gider; controller ve SQLite işlemleri kendi event-loop thread'inde kalır. Gerçek route çağrısı ve paralel health/state yanıtı bu ayrımı sınamalıdır.
- **Idempotency kapsamı:** AOS unique anahtarı `(session_id, idempotency_key)` iken aynı ham anahtarı tek Lab service owner'a göndermek iki oturumu çakıştırır. Remote key'in namespace'i ve immutable eşlemesi gereklidir.
- **Eksik kaynak görüntüsü:** ilk test kopyasında önceki AOS migration'ları bulunmadı. Migration dizini before/after hash ile yeniden alındı; eşzamanlı değişim yakalanan ilk kopyalama başarı sayılmadı. `review-evidence/aos-migrations-snapshot.json` bu hazırlığın kaydıdır; paket kabulü değildir.

Bu sorunlar için uygulama devam eder. Prototip client/coordinator testleri gerçek AOS task/tool/policy, Lab kuyruk dispatch'i veya GPU/model kabulünün yerine geçmez.

## Prototip testleri ve terminal durum incelemesi

Ayrı AOS kopyasının Python 3.12.13 ortamında hedefli 8 test ve 3.760 paket kontrolü exit 0 verdi. `aos-lab-external-job-prototype.json` ilk kaydındaki Python 3.14.7 alanı test yorumlayıcısıyla uyuşmaz; yeniden üretilmesi istenmiştir. Bu testler sahte Lab taşıması, gerçek loopback JSON ve özel SQLite fixture'ları kapsamındadır.

Root incelemesi `review_aos_external_terminal.py` ile kaynaklar sabitken iki hatayı tekrar üretti (exit 1, 2026-09-24 19:39 UTC):

- Daha önce tamamlanmış koşuya dönen idempotent start ACK, rapor alınmadan `completed` olarak sunuldu. Rapor hash'i yoktu; rapor çağrısı ve verification sayısı sıfırdı.
- Gecikmiş `running` yanıtı, daha yeni ve raporu doğrulanmış `completed` kaydını geri aldı ve rapor hash'ini sildi. AOS run satırı ise `succeeded` kaldı; iki kayıt birbiriyle çelişti.

Negatif kanıt `review-evidence/aos-external-terminal-before.json` içinde saklanır. Prototip modül hash'i `e3d5795b14027f6aa51b762d48f62e3ee24e112e10a0bcef6525f1b66e678808`; iki test de gerçek SQLite/coordinator ile kontrollü sahte taşıma kullanır. Canlı AOS, Lab sunucusu, model veya GPU çalıştırılmadı. Düzeltme ve sonraki kaynaklara bağlı doğrulama beklenir; terminal ACK tek başına başarı kanıtı değildir ve eski yanıt terminal kayıtları geri alamamalıdır.

### Gerçek raporun taşıma sınırı

`review_aos_report_transport.py`, aynı sabit istemci kaynağını kontrollü loopback HTTP sunucusunda sınadı (19:42 UTC, exit 1). Önceden tamamlanmış CLI20 koşusunun hash'i doğrulanmış 80-skorlu Scorer raporu API zarfında **70.876 bayt** oldu; istemcinin 65.536 bayt sınırı bu geçerli raporu reddetti. Küçük geçerli rapor kabul edildi, bozuk SHA reddedildi. Ayrıca zarf run kimliği doğru fakat rapor gövdesi farklı run'a ait olan, kendi SHA'sı geçerli belge yanlışlıkla kabul edildi. Negatif kanıt `review-evidence/aos-report-transport-before.json`; yeni araştırma veya gerçek Lab sunucusu çalıştırılmadı. Rapor için kaynak bütçesine bağlı taşıma boyutu ve bütün run/status/hash bağlarının doğrulanması gereklidir.

## Gerekli kanıtlar

### Düzeltme sonrası doğrulama

19:47 UTC tekrarında terminal sürücüsü **2/2**, rapor taşıma sürücüsü **4/4** kontrolle exit 0 verdi. Her iki koşuda prototip kaynağı sabitti: `8565994c0aa2ddbc9af45e25ac4e31dcffe2191a2597c1ee8791dd7f5fca4dcb`. Start ACK ardından status/rapor doğrulanıyor; HTTP bekleyişi sonrası güncel SQLite satırı okunarak terminal durum ve hash korunuyor. Rapor gövdesindeki run kimliği ve status yanıtındaki rapor hash'i de eşleniyor. Yeni rapor sınırı 256 KiB, diğer kontrol yanıtları 64 KiB; 70.876 baytlık gerçek rapor artık geçiyor.

Sonuçlar `aos-external-terminal-review.json` ve `aos-report-transport-review.json` içindedir; negatif `*-before.json` kayıtları korunur. Bunlar mevcut 80 ölçümlük raporu kanıtlar. İzin verilecek suite/proposal üst sınırının rapor boyutuna bağlanması veya sayfalama hâlâ API entegrasyon işidir; daha büyük bütün süitlerin 256 KiB'a sığdığı ileri sürülmez. Gerçek AOS policy yolu, API→Director ve birlikte GPU kullanımı açık kalır.

Güncel prototip kanıtı Python 3.12.13 alanını düzeltti; hedefli testler 10/10, ayrı kopyanın paket doğrulaması 3.838 kontrolle exit 0. Birleşik yama başlıkları düzeltildi ve `git apply` ayrıştırması geçti. Root paket incelemesi 8 dosyanın geçici staging alanında yama sonrası hash'lerini doğruladı. İlk büyük MANIFEST farklarının bağımsız hash değişiklikleri olmadığı, sıralama farkı olduğu görüldü; özgün satır sırası korunarak teslim farkı yalnız entegrasyon girdilerine indirildi (7 ekleme/1 silme). Yama SHA `c6708c3d53a8410d5fc7e6adcafe6704618d8e1148dc4809ff6ea3dfd6f58bf2`; `aos-patch-packaging-review.json` taban, teslim ve ayrı kopyanın doğrulama manifestlerini ayırır. Bu, canlı AOS'a yama uygulama veya onun güncel paket kabulü değildir.

### Tam entegrasyon kanıtları

Gerçek store açılışı da ayrıca sınandı: `review_aos_store_restart.py`, üretim `TrajectoryStore` ile özel disk SQLite'ı açtı, sahte controller/Lab taşımasıyla aktif dış iş oluşturdu, store'u kapatıp yeniden açtı. Koşu `running` olduğu halde `runtime_states` kaydı yoktu; AOS'un gerçek `reconcile()` yolu **`AOSFault: Unknown run`** ile açılışı durdurdu (19:50 UTC, exit 1). `aos-store-restart-before.json` sabit kaynaklara bağlı negatif kanıttır. Typed task/state entegrasyonu bu aktif işin yeniden açılışını ve yetki yenilemesini tamamlamalıdır; genel AOS recovery denetimini atlamak çözüm değildir. Test kendi DB/lock dosyalarını temizledi; canlı AOS kullanılmadı.

API→Director testi için `review_prepare_api_suite.py` dört özel sentetik dev profili ve 512 Scorer etiketi hazırladı; üretim Planner loader 4/4 görevi doğruladı (`api-suite-preparation.json`). Fixture `49e04b62-f436-4242-9e1c-4a4e208ac9d7`, suite `synthetic.api-integration.v1`. Bu hazırlık hiçbir `lab.runs` satırı oluşturmadı/değiştirmedi: koşunun kabulü ve queued→running geçişi gerçek API/yürütücüden gelmelidir. Fixture kullanımı bitince receipt içindeki cleanup komutu yalnız bu profilleri siler; bağlı görev planı varsa reddeder.

| Kontrol | Kabul kanıtı |
|---|---|
| Gerçek typed giriş | AOS görev kataloğundan izinli eylem, state/lease/policy kararı, gerçek task/run/step/action satırları |
| API → Director | AOS start sonrası manuel SQL geçişi olmadan gerçek Docker/Scorer ölçümü ve terminal Lab raporu |
| Yeniden deneme | Yanıt kaybı, aynı ve farklı payload, iki session, yanlış owner/lease/tool, restart; tek doğru Lab koşusu |
| Foreground kullanılabilirliği | Lab arka plan işi sürerken başka bir yetkili AOS görevinin ölçülmüş ilerlemesi; kontrol çağrılarında sınırlı gecikme |
| Rapor doğrulaması | Yanlış run/hash/status ve eksik rapor reddi; doğru raporun AOS verification ve trajectory kaydı |
| İzolasyon | Ayrı AOS test DB/port/workspace/env; canlı AOS'un değiştirilmediği kaynak ve süreç kaydı |
| Paket | AOS'un kendi test ortamında hedefli testler ve package validation; Lab kalite kapısı gerçek exit 0 |

Gerçek yerel model, adil GPU dilimleri, worker drain ve ortak RAM/CPU/disk ölçümleri M0.AOS.4–7 kapsamında ayrıca gereklidir. CPU prototipi bu maddeleri kapatmaz.
