# AOS → Lab API ve Director bağlantısı

Bu dilim, ayrı AOS test kopyasındaki istemcinin gerçek Lab HTTP sunucusuna
bağlanmasını ve API kuyruğundan araştırma yürütücüsüne geçişi ele alır.
Canlı AOS kaynakları veya servisi değiştirilmez.

## Gerçek kontrol bağlantısı

`review-evidence/review_api_wire.py --config-dir <özel yapılandırma dizini>`
2026-09-24'te exit 0 verdi; **21/21 kontrol** geçti.
[Kayıt](review-evidence/api-control-wire-review.json), çalıştırılan komutları,
kaynak hash'lerini, kabul edilen isteği ve kontrol sonuçlarını tutar.

- Lab sunucusu Lab ortamında gerçek loopback TCP portunda çalıştı. API süreci
  özel systemd biriminde 1 GiB RAM, sıfır swap ve bir CPU kotasıyla başlatıldı.
- Gerçek `LabApiClient`, AOS'un ayrı Python ortamında POST/status çağrıları yaptı.
- PostgreSQL'de `origin=aos`, principal kaynaklı owner ve AOS task/run/action
  kimlikleri aynı istekle saklandı. İstemci kimliği kendi başına belirleyemiyor.
- Aynı anahtar/istek aynı koşuyu döndürdü. Değişmiş istek ve aynı AOS eylemine
  yeni anahtarla ikinci koşu açma girişimi 409 ile reddedildi.
- Başka principal'ın status/stop/report erişimi 404, yetkisiz POST 401 aldı.
  Kayıtsız süit, kayıtlı proposal tavanını aşma ve eksik AOS kimliği reddedildi.
- Kuyruktaki ikinci koşuya tekrarlı stop uygulandı. POST kendi başına deney
  çalıştırmadı: deney ve skor sayıları sıfır kaldı.
- İki özel koşu satırı ve test sunucusu temizlendi. Hazırlanmış dört sentetik
  profil sonraki Director denemesi için saklanıyor. Kaynaklar testte değişmedi.

Bu sonuç AOS'un gerçek task/tool/policy yürütmesini, lease/takeover/restart
kabulünü, model/GPU paylaşımını veya bitmiş bir araştırma raporunu kanıtlamaz.
Süreç kotaları bu testin başlatıcısına aittir; üretim servis başlatma ve toplam
kaynak bütçesi ayrıca doğrulanmalıdır.

Araştırma sürerken ayrıca gerçek sağlık/status/tekrarlı queued-job stop
çağrıları yapıldı: [aktif kontrol kaydı](review-evidence/api-active-controls-review.json)
3/3 kontrolle exit 0 verdi. Aynı anda canlı API/Director systemd süreçleri ve
Scorer tarafından yazılmış araştırma skorları doğrulandı. Beş çağrının en uzunu
2,24 ms sürdü; hedeflenen diğer işin stop durumu korunurken araştırma `running`
kaldı. Bu, aktif araştırmanın ortasında iptal/recovery testi değildir.

## Kod ve yama

Yerel ve AOS principal'ları özel yapılandırmadan yüklenir; kayıtlı süitler
dosya hash'lerine bağlanır. `0015_external_run_mapping` geçişi, AOS kimliklerini
immutable istekle eşler ve aynı owner/action için ikinci koşuyu engeller.
`lab api serve`, `lab director dispatch-one` ve arka plan `lab director drain`
komutları eklendi. Örnek kullanıcı servisleri `ops/systemd` altındadır.

AOS adapter değişikliği [ayrı yamada](review-evidence/aos-lab-api-mapping.patch)
saklıdır. [Paketleme kaydı](review-evidence/aos-lab-api-mapping.json) eklenen altı
dosyanın uygulanıp byte eşliğinin doğrulandığını ve mevcut dosya bağlamlarının
önceki incelenmiş yamayla aynı kaldığını gösterir. Canlı AOS'a uygulanmadı;
tam typed/policy/restart entegrasyonu değildir.

0.14.0 imajı `sha256:bdd9733e8d83be913e8c49c87295034439658458b358734eabfa3dc904a02ef1`
üzerinde kaynak byte eşliği geçti. Tam kalite kapısı exit 0: 176 test geçti,
11 live/GPU testi seçilmedi; strict mypy 63 kaynak, Ruff/Bandit ve wheel/import
kontrolleri geçti; Pylint 9.30/10. [Değişmez gate/imaj bağı](review-evidence/api-dispatch-quality-gate-binding.json)
bu kanıtın hash ve sürümünü tutar.

## Araştırma yürütme kabulü

`review_api_wire.py --execute` **exit 0, 30/30 kontrol** ile tamamlandı.
[Gerçek koşu kaydı](review-evidence/api-director-wire-review.json), çalışma
komutlarını, systemd kaynak ölçümlerini, terminal belgeleri ve raporu içerir.
Koşu `dc3c31c7-7eb6-4164-9766-5af4fb2a67b1`; oluşturma ve queued→running geçişi
ürün API/Director'a aittir. Test SQL ile koşu oluşturmadı veya durum değiştirmedi.

- Üç baseline × dört görev × üç tohum = 36 bağımsız skor; aday için dört görev
  × üç tohum = 12 skor. Dört deney ve 48 skorla koşu `completed` oldu.
- Kaynak kodu veren sahte sağlayıcının ilk önerisi ölçümlerle `KEEP` aldı.
  Karar, delta ve güven aralığı gerçek Scorer satırlarından yeniden hesaplandı;
  bit eşliği geçti. Aday/provider hazır skor veya karar vermedi.
- Dört experiment/trajectory çifti ve aday/girdi/mesaj blobları fiziksel olarak
  hash doğrulandı. Görev planı 48 ölçümle kapatıldı; ayrı finalizer raporu yayımladı.
- Ayrı AOS Python ortamındaki gerçek istemci terminal raporu okudu ve doğruladı.
  Rapor SHA: `c167c903ca65471686866ac11337c13a09aa0acaef300d86d61f12fbd1d91ed7`.
- Yaklaşık 589 saniyelik gözlemde 295 sağlık/status örneği alındı. En yüksek
  sağlık gecikmesi 1,34 ms; status 2,29 ms; status p95 1,84 ms idi.
- Systemd'de API 1 GiB, Director 2 GiB RAM; her biri bir CPU ve sıfır swap
  sınırı doğrulandı. Örneklenmiş memory peak API 161.931.264, Director
  204.398.592 byte idi. Bunlar yalnız bu iki birimi kapsar; tüm host/GPU/AOS
  toplam tüketimi kabulü değildir. Test birimlerinin TasksMax değeri 128'dir;
  teslim edilen servis şablonlarının 32-task profili bu koşuda kullanılmadı.
- Kaynak/harness hash'leri sabit kaldı. İki özel run satırı ve test servisleri
  temizlendi; belge blobları saklandı. Dört sentetik profil sonraki typed AOS
  entegrasyonu için özel test alanında tutuluyor.

Bu yol gerçek AOS task/tool/policy döngüsü değildir. Yerel model, GPU/eğitim,
public veri, canlı AOS birlikte çalışma ve tam restart/takeover kabulü açık.
Kalıcı bozuk bir registry girdisinin kuyruk başında kalması ve koşu başladıktan
sonraki hata/recovery yaşam döngüsü de tamamlanmış sayılmaz.

## Tarihsel kanıtların korunması

0.13.0 stop-admission kalite kapısı, yeni gate sonucu yazılmadan önce byte
eşliğiyle `evidence/quality-gate-stop-admission.json` dosyasına alındı;
`stop-admission-quality-gate-binding.json` artık bu değişmez kaydı gösterir.
İmaj parite kaydı `review-evidence/stop-admission-image-review.json` içinde
korunur. Önceki CLI20 kabulü hâlâ kendi 0.12.0 kaynak/imaj/gate bağını taşır.
