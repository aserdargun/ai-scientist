# Native araştırma kapanışı: iki dar düzeltme

## Gerçek koşudaki bulgu

`6dd84567-09f7-4fd1-b427-e80c9d17bb28` AOS üzerinden açıldı.
Yerel model öneri üretti; üç baseline için dokuz ölçüm ve aday için bir
primary ölçüm tamamlandı. Koşu sentetik çalışma modu verisi kullanır.
Tamamlanmış araştırma veya Public27 kabulü değildir.

Aday çıktısı koşuya özel artifact dizininde doğru SHA ile mevcuttu.
Terminal replay oluşturucu varsayılan dizinden okumaya çalıştığı için
`FileNotFoundError` oluştu. Üst katman bunu infrastructure exception olarak
korudu; Referee terminal kaydı ve araştırma raporu oluşmadı.

Bu exception sonrasında SQL kapanışı `stop_requested` durumunu ve
`run.dispatch_recovery_required` event'ini yazdı. Recovery'nin ilk stop
zamanı için aradığı `run.stop_requested` event'i yoktu. Bu nedenle güvenli
kapanış `first_stop_deadline_unavailable` ile bekledi. Bütçe aşımı
kanıtlanmış değildir; stop yaklaşık 360 saniyede gözlendi.

## Uygulanan değişiklikler

- `lab/director/runner.py`: terminal replay çıktısı okumaya mevcut
  `artifact_root` aktarılır. SHA denetimi, Referee kararı ve bütçe korunur.
- `0042_failure_stop_event`: exception stop geçişi ve canonical ilk stop
  event'i aynı transaction/zaman altında yazılır. Mevcut rol, owner,
  generation, invocation ve execution kontrolleri korunur. Aynı isteğin
  tekrarı ilk zamanı değiştirmez. Eski eksik-event satırları onarılmaz;
  yeni deadline veya genişletilmiş kaynak tahsis yetkisi verilmez.

## Doğrulama

Artifact regresyonu eski kodda gerçek dosya okumasıyla `FileNotFoundError`
yakaladı; düzeltmeyle ilgili 11 test geçti. SQL regresyonu 0041 üzerinde
eksik ilk-stop event nedeniyle başarısız oldu. 0042 ile ayrı PostgreSQL
veritabanında gerçek rollerle 14 kontrol geçti; fixture konteyneri ve erişim
dosyaları temizlendi. Bunlar yeni gerçek GPU/araştırma kabulü değildir.

[SQL regression kanıtı](review-evidence/failure-stop042-sql.json),
[gerçek native koşunun kısmi sonucu](review-evidence/native-aos-partial-acceptance-20261002.json).

## AOS'a aktarım ve sıradaki gerçek koşu

AOS kaynakları ve aktif scope değiştirilmedi. Ortak runtime sözleşme
sürümleri değişmedi: control/runtime/terminal v1, profile-output v2.
Yeni test veritabanı schema head'i `0042_failure_stop_event` olmalıdır.
Scientist kaynak farkı ve kalite kapısı sonucu AOS karşı incelemesine verilir.

Sonraki kabul yeni, sınırlı koşuda yapılır: AOS görev başarısı → kanıtlı GPU
devri → Scientist öneri/bağımsız karar/terminal rapor → AOS rapor hash ve
readback → fiziksel cleanup. Adil eşzamanlı ilerleme ve kontrollü iptal ayrıca
kanıtlanır. Eski koşu reset edilmez veya yeniden başlatılmaz; süresi dolmuş
cleanup yetkisi uzatılmaz. Aktif kullanıcı işiyle çakışma varsa GPU koşusu
başlatılmaz.

AOS karşı incelemesi iki kaynak hash'ini bağımsız doğruladı ve salt okunur
incelemeyi başarılı kaydetti. Karşı inceleme receipt SHA256:
`117c49ee93448541336342554aeb351e2a8e5e959414a563b07ba31da66f3c44`.
AOS bu incelemede migration veya yeni GPU koşusu yürütmedi.
Eski native paketteki 143 input pin'i değişmemiştir; yeni araştırma
kaynakları sonraki kabul paketinde ayrıca bağlanmalıdır.

Son kaynak kapısı: 2854 passed / 7 skipped / 149 deselected, yedi komutun
her biri exit 0; mypy 166 dosya ve wheel import smoke başarılı.
[Kaynak kapısı](evidence/quality-gate-latest.json).
İlk başlatma PATH eksikliği, ardından RAM diski reserve sınırı ve uzun
UNIX socket yolu nedeniyle başarısız kapı kayıtları private olarak korundu.
Başarılı kapı disk üzerindeki kısa `data/runtime/q42` pytest basetemp ve
`data/runtime/q42tmp` TMPDIR ile çalıştı; ürün korumaları gevşetilmedi.
