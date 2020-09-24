import selenium
from selenium import webdriver
from selenium.webdriver.common.by import By
from getpass import getpass
def test():
    # Login to the browser
    driver = webdriver.Remote('http://localhost:4444/wd/hub', webdriver.DesiredCapabilities.FIREFOX)
    driver.set_window_size(1280, 1024)
    driver.get("https://timisoara.alt-f4.ro/")
    driver.find_element_by_id("id_username").send_keys(input("Enter username:"))
    driver.find_element_by_id("id_password").send_keys(getpass("Enter password:"))
    driver.find_element_by_xpath('//button[text()="login"]').click()

    try:
        print(driver.find_element_by_class_name("alert-danger").text)
        return
    except:
        pass
    app_links = driver.find_elements(By.XPATH,"//a[@href]")
    valid_links = list(map(lambda x : x.get_attribute("href"), app_links[2:]))
    print(valid_links)

    hoover_link = valid_links[0]
    print(hoover_link)
    driver.get(hoover_link)
    print(driver.page_source.encode("utf-8"))


if __name__ == "__main__":
    test()