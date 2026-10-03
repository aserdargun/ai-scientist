# Native R7: gerçek inflight stop, SQL039 düzeltmesi ve quarantine

R7 run `653849d5-74c4-4ddb-aeb6-dd12631aa7dc`; başlangıç Scientist
`31cd67dbcebb096e4e68e478470acbc07c74e62e`, kaynak kodu `4e1bb41`.
R6 kayıtları korunarak yeni SQL0038 veritabanı ve yeni istek kullanıldı:
2400 saniye / 1 deney / 24576 token rezervasyonu. Dört sentetik aile,
256 train / 384 eval örneği; fake-json provider. İlk öneri gerçek robust-z
ölçek çarpanını 1.1 yapar; production initial coverage `hparam` seçimiyle
eşleşmesi, source derlemesi ve scenario hash'i koşudan önce doğrulandı.
Gerçek model/quantization veya öğrenilmiş adaptör yoktur.

## Geçen

Üç baseline, 36 gerçek bağımsız Scorer hücresi ve frozen calibration geçti.
G1 kontrollü crash, normal G2 resume ve gecikmiş G1 retleri geçti. Bu kez
bir gerçek primary job admitted oldu, gerçek Scorer invocation claim aldı
ve **iş running iken** typed API stop istendi. Tekrarlı stop ilk isteğin
deadline'ını korudu. Stop ACK tek başına tamamlanma kanıtı sayılmadı.

## Başarısız

Genel koşu exit1, 1273.871664 saniye; kaynak hash'leri değişmedi.
Kapanış `ProgrammingError`, PostgreSQL SQLSTATE `42703` verdi:
SQL0037 `assert_attempted_proposal_shape` içinde `task_scores` tablosunda
bulunmayan `s.admitted_generation` ve `s.execution_sha256` okunuyordu.
Ledger `stop_requested`, rapor NULL; proposal `primary_running`, bir primary
job ledger'da running, stop marker0. Automatic recovery pending kaldı.
Child/final seal, üç missing-cell completion ve terminal stopped report
halen kabul edilmedi.

## Düzeltme ve gerçek SQL regresyonu

Ek SQL0039 migration yalnız bu hatalı identity parçasını değiştirir.
Kimlik bağlı immutable score job `j` üzerinden alınır. Proposal için exact
current generation, baseline için restart ancestry, her job için execution,
normal Scorer canlı claim/invocation INSERT fences ve tüm score/cell/profile/
harness/completion kontrolleri korunur. Temizlenen completed-job invocation
alanına yanlış bir eşlik eklenmez. Eski SQL0037/0038 dosyaları değişmedi.

Önce aynı **gerçek R7 kayıtları** üzerinde read-only pure shape kontrolü
`42703` hatasını yeniden üretti. Sonra migration yalnız bu oturumun izole
R7 veritabanına normal Migrator rolüyle uygulandı. Aynı helper aynı kayıtlar
üzerinde geçti. Run/job/score/contract/recovery snapshot'ı birebir aynı kaldı;
hiçbir status, owner, generation, bütçe veya deadline değiştirilmedi.
Bu kontrol terminal stop veya tam yaşam döngüsü kabulü değildir.

SQL schema/provenance regresyonu dört testte geçti. Son zorunlu kalite kapısı
yedi komutta exit0: **1444 geçti / 7 opt-in atlandı / 120 GPU/live seçilmedi**.
CLI artık allowlist edilmiş beş karakterli SQLSTATE'i hata cevabına ekler;
SQL metni, parametreler veya raw exception mesajı açığa çıkarılmaz.

## Fiziksel kapanış ve korunan quarantine

Bağımsız root kontrolü exit0: exact G1/G2 sahipleri ölü ve cgroup'ları boş;
37 Scorer işçisinin tamamı fiziksel olarak quiescent. Primary işçinin
özgün job/invocation eşliği tekrar doğrulandı. Parent/API inactive/MainPID0.
Recovery worker başlamadı; actor kayıtları0. Ana servisler/AOS değiştirilmedi.

Özgün 120 saniyelik stop penceresi dolduğundan yeni closure/deadline yaratılmadı.
Ledger running job1 ve `stop_requested` kaydı **quarantine olarak korundu**;
veritabanı recovery incelemesi için tutuldu. Fiziksel quiescence ledger
terminali veya GPU release kabulü değildir. Bu CPU koşusunda GPU tahsisi yoktu.

## Kalan ve çalıştırılmayan

- Yeni SQL039 koşusunda running → stop_requested → doğrulanmış stopped rapor;
  planned missing-cell closure ve recovery actor retirement/final seal.
- Gerçek partial-CAS worker crash/retry ve drained proof sonrası ledger retry.
- AOS ortak version/capability/principal sözleşmesi, scheduler rezervasyonu ve
  tek koordineli gerçek GPU devri. VRAM tepe ve GPU devir gecikmeleri ölçülmedi.

M0 sayısı artırılmadı; baseline/sentetik kanıt gerçek model araştırması değildir.
AOS'a aktarılacak güncel sınırlar: [runtime hazırlığı](70-aos-runtime-readiness.md).
Lisans ve genel CI işleri ayrı tutulur. Push/merge/deploy yapılmadı.
